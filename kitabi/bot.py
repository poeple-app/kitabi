"""Telegram bot logic for Kitabi (v0.1.1)."""

from __future__ import annotations

import functools
import html
import io
import os
import urllib.parse
import zipfile
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable, Callable

import structlog
from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from . import ai, data
from .data import BookStatus, Category, Session, to_local

logger = structlog.get_logger(__name__)
ScreenResult = tuple[str, InlineKeyboardMarkup]


# ────────────────────────── helpers ──────────────────────────


def esc(text: object) -> str:
    """HTML-escape a value for inclusion in a Telegram HTML message."""
    return html.escape(str(text) if text is not None else "")


def _cb(*parts: Any) -> str:
    return ":".join(str(p) for p in parts)


def BTN(label: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=action)


def KB(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def _nav_row(include_back: bool = True) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if include_back:
        row.append(BTN("⬅️ Geri", "back"))
    row.append(BTN("🏠 Ana Menü", "main"))
    return row


def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "kitabi"


async def _send_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup,
    *,
    photo_url: str | None = None,
) -> None:
    """Edit-in-place when triggered by callback; new bubble when user sent input.

    Handles photo↔text transitions by delete+send (Telegram can't edit between).
    """
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception as e:
            logger.debug("bot.send_screen.answer_failed", error=str(e))
        msg = update.callback_query.message
        try:
            if photo_url:
                await msg.delete()
                await context.bot.send_photo(
                    chat_id=msg.chat_id, photo=photo_url,
                    caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML,
                )
            elif msg.photo:
                await msg.delete()
                await context.bot.send_message(
                    chat_id=msg.chat_id, text=text, reply_markup=keyboard,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
            else:
                await msg.edit_text(
                    text, reply_markup=keyboard,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
        except Exception as e:
            logger.warning("bot.send_screen.fallback", error=str(e))
            await context.bot.send_message(
                chat_id=msg.chat_id, text=text, reply_markup=keyboard,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
    elif update.message:
        if photo_url:
            await update.message.reply_photo(
                photo=photo_url, caption=text,
                reply_markup=keyboard, parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                text, reply_markup=keyboard,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )


# Persistent chat-state shims (over data.EphemeralState)
async def _get(chat_id: int, key: str) -> Any:
    return await data.get_ephemeral(chat_id, key)


async def _set(chat_id: int, key: str, value: Any, ttl_s: int = 1800) -> None:
    await data.set_ephemeral(chat_id, key, value, ttl_s=ttl_s)


async def _clear(chat_id: int, *keys: str) -> None:
    await data.clear_ephemeral(chat_id, *keys)


def _chat_id(update: Update) -> int | None:
    if update.effective_chat:
        return update.effective_chat.id
    if update.message:
        return update.message.chat_id
    if update.callback_query and update.callback_query.message:
        return update.callback_query.message.chat_id
    return None


async def _err_reply(msg, exc: Exception, prefix: str) -> None:
    """Single-line error reply pattern used across input handlers."""
    await msg.reply_text(
        f"❌ {prefix}: <code>{esc(type(exc).__name__)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def _prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    awaiting: dict[str, Any] | None = None,
) -> None:
    """Show a prompt screen and (optionally) set the chat into an awaiting state."""
    if awaiting is not None:
        cid = _chat_id(update)
        if cid is not None:
            await _set(cid, "awaiting", awaiting)
    await _send_screen(update, context, text, KB([_nav_row()]))


def _build_draft(
    session: Session,
    book: data.Book | None,
    transcript: str,
    category: Category,
    page: int | None,
    definition: str | None = None,
    explanation: str | None = None,
) -> dict[str, Any]:
    """Common draft-note dict used by voice/photo/text handlers."""
    return {
        "book_id": session.book_id,
        "book_title": book.title if book else "?",
        "book_short_code": book.short_code if book else "",
        "session_id": session.id,
        "transcript": transcript,
        "category": category.value,
        "page": page,
        "definition": definition,
        "explanation": explanation,
    }


async def _transcribe_voice_msg(msg) -> str:
    """Download a Telegram voice message and run Gemini ASR over it."""
    voice_file = await msg.voice.get_file()
    audio_bytes = await voice_file.download_as_bytearray()
    return await ai.transcribe_voice(bytes(audio_bytes), msg.voice.mime_type or "audio/ogg")


# ────────────────────────── error decorator ──────────────────────────


def _logs_url(event_name: str) -> str | None:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    service = os.environ.get("K_SERVICE")
    if not project:
        return None
    parts = ['resource.type="cloud_run_revision"']
    if service:
        parts.append(f'resource.labels.service_name="{service}"')
    parts.append(f'jsonPayload.event="{event_name}"')
    encoded = urllib.parse.quote(" AND ".join(parts), safe="")
    return (
        f"https://console.cloud.google.com/logs/query"
        f";query={encoded};duration=PT24H?project={project}"
    )


def _safe_handler(
    func: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Catch every handler exception, log it, and surface a friendly error+log link."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      *args: Any, **kwargs: Any) -> Any:
        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:  # noqa: BLE001
            ec = type(e).__name__
            uid = update.effective_user.id if update.effective_user else None
            logger.error(f"bot.handler.{func.__name__}.failed",
                         error=str(e), error_type=ec, user_id=uid,
                         handler=func.__name__, exc_info=True)
            evt = f"bot.handler.{func.__name__}.failed"
            url = _logs_url(evt)
            logs_line = (
                f'📋 <a href="{esc(url)}">Bu hatanın detay loglarını aç</a>\n\n'
                if url else f"📋 Cloud Run loglarında <code>{esc(evt)}</code> event'ini ara.\n\n"
            )
            user_message = (
                "❌ <b>Beklenmedik bir hata oluştu.</b>\n\n"
                f"İşlem: <code>{esc(func.__name__)}</code>\n"
                f"Hata: <code>{esc(ec)}</code>\n\n"
                "Tekrar denemeyi unutma — geçici bir sorun olabilir.\n\n"
                + logs_line
                + 'Sorun devam ederse <a href="https://github.com/poeple-app/kitabi/issues">'
                "GitHub Issues</a> üzerinden bildirebilirsin."
            )
            try:
                if update.callback_query:
                    try:
                        await update.callback_query.answer(f"Hata: {ec}", show_alert=False)
                    except Exception:
                        pass
                    await context.bot.send_message(
                        chat_id=update.callback_query.message.chat_id,
                        text=user_message, parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                elif update.message:
                    await update.message.reply_text(
                        user_message, parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
            except Exception as inner:
                logger.error("bot.handler.error_report_failed",
                             original_handler=func.__name__,
                             inner_error=str(inner), exc_info=True)
    return wrapper


# ────────────────────────── screen builders ──────────────────────────


async def screen_main() -> ScreenResult:
    """Main menu — adapts to library state."""
    books = await data.list_books()
    open_sessions = await data.list_open_sessions()
    if not books:
        text = (
            "🏠 <b>Ana Menü</b>\n\n"
            "Henüz hiç kitabın yok. Her şey kitap eklemekle başlıyor.\n\n"
            "Ekledikten sonra bot sana neler yapabileceğini gösterir."
        )
        return text, KB([[BTN("➕ İlk Kitabını Ekle", "newbook:start")]])
    head = (f"🟢 {len(open_sessions)} oturum açık." if open_sessions
            else f"📚 {len(books)} kitap kütüphanende.")
    kb = []
    if open_sessions:
        kb.append([BTN(f"🟢 Açık Oturumlar ({len(open_sessions)})", "open_sessions")])
    kb += [
        [BTN("▶️ Oturum Başlat", "start_pick")],
        [BTN("📚 Kitaplarım", "books"), BTN("➕ Yeni Kitap", "newbook:start")],
        [BTN("🔍 Ara", "search:start"), BTN("📖 Sözlük", "glossary")],
        [BTN("💬 Alıntılar", "quotes:all"), BTN("📊 İstatistik", "stats")],
        [BTN("📤 Dışa Aktar", "export:menu"), BTN("⚙️ Ayarlar", "settings")],
    ]
    return f"🏠 <b>Ana Menü</b>\n\n{head}", KB(kb)


def screen_onboarding_after_first_book(book: data.Book) -> ScreenResult:
    text = (
        f"🎉 <b>{esc(book.title)}</b> kütüphanene eklendi (kod: <code>{esc(book.short_code)}</code>).\n\n"
        "<b>Bot şunları yapabiliyor:</b>\n\n"
        "🎤 <b>Sesli not</b> — bir oturum açtıktan sonra ses kaydı at, "
        "Gemini transkript eder, kategori önerir.\n\n"
        "📷 <b>Sayfa fotoğrafı</b> — sayfayı çek, OCR yapılır + sayfa numarası "
        "okunmaya çalışılır.\n\n"
        "✍️ <b>Düz metin</b> — yazarsan da olur.\n\n"
        "❓ <b>Soru sor</b> — okurken aklına takılan bir şeyi sor, "
        "Gemini bu kitabın notlarını bağlam alarak cevaplar.\n\n"
        "🏁 <b>Bitir</b> — kitabı bitirince puan + tek cümlelik yorum + "
        "favori alıntılarınla bir PDF okuma günlüğü üretir.\n\n"
        "<i>Şu anda yapabileceğin tek şey okumaya başlamak.</i>"
    )
    return text, KB([
        [BTN("▶️ Bu kitabı okumaya başla", _cb("recap", book.id))],
        [BTN("➕ Başka kitap ekle", "newbook:start")],
        [BTN("🏠 Ana Menü", "main")],
    ])


async def screen_book_list() -> ScreenResult:
    books = await data.list_books()
    if not books:
        return (
            "🏠 Ana › 📚 <b>Kitaplarım</b>\n\nHenüz kitabın yok.",
            KB([[BTN("➕ Yeni Kitap Ekle", "newbook:start")], _nav_row(False)]),
        )
    text = f"🏠 Ana › 📚 <b>Kitaplarım</b>\n\n{len(books)} kitap. Detay için birine bas:"
    kb = []
    for b in books:
        icon = _STATUS_ICONS.get(b.status, "📖")
        suffix = f" — %{int(100 * (b.read_pages or 0) / b.total_pages)}" if b.total_pages else ""
        kb.append([BTN(f"{icon} {b.short_code} · {b.title}{suffix}", _cb("book", b.id))])
    kb.append([BTN("➕ Yeni Kitap", "newbook:start")])
    kb.append(_nav_row(False))
    return text, KB(kb)


_STATUS_ICONS = {
    BookStatus.FINISHED: "✅", BookStatus.READING: "📖",
    BookStatus.PAUSED: "⏸️", BookStatus.NOT_STARTED: "🆕",
}


async def screen_book_detail(book_id: int) -> tuple[str, InlineKeyboardMarkup, str | None]:
    book = await data.get_book(book_id)
    if not book:
        return "Kitap bulunamadı.", KB([_nav_row(False)]), None

    pct = int(100 * (book.read_pages or 0) / book.total_pages) if book.total_pages else 0
    lines = [
        f"🏠 Ana › 📚 Kitaplarım › <b>{esc(book.title)}</b>",
        "",
        f"📖 <b>{esc(book.title)}</b>  ·  <code>{esc(book.short_code)}</code>",
    ]
    if book.author:
        lines.append(f"✍️ {esc(book.author)}")
    if book.genre:
        sub = f" › {esc(book.subgenre)}" if book.subgenre else ""
        lines.append(f"🏷️ {esc(book.genre)}{sub}")
    if book.isbn:
        lines.append(f"🌐 ISBN: {esc(book.isbn)}")
    lines += [
        "",
        f"📊 {book.read_pages or 0} / {book.total_pages or '?'} sayfa (%{pct})",
        f"📅 {len(book.sessions)} oturum",
        f"📝 {len(book.notes)} not",
        f"🔖 Durum: {esc(book.status.value)}",
    ]
    if book.bought_from:
        bought = f"🛒 {esc(book.bought_from)}"
        if book.price_tl:
            bought += f" · {book.price_tl} TL"
        if book.bought_at:
            local = to_local(book.bought_at)
            bought += f" · {local:%d %b %Y}" if local else ""
        lines += ["", bought]
    if book.tags:
        lines.append("🏷️ " + " ".join(f"#{esc(t)}" for t in book.tags))
    if book.personal_note:
        lines += ["", f"<i>“{esc(book.personal_note)}”</i>"]
    if book.rating:
        lines += ["", "⭐" * book.rating + f"  ({book.rating}/5)"]
    if book.one_line_review:
        lines.append(f"<i>{esc(book.one_line_review)}</i>")

    kb: list[list[InlineKeyboardButton]] = []
    if book.status == BookStatus.FINISHED:
        kb.append([BTN("📕 PDF üret", _cb("export", "pdf", book.id))])
        kb.append([BTN("🔁 Yeniden Oku", _cb("recap", book.id))])
    else:
        kb.append([BTN("▶️ Okumaya Devam Et", _cb("recap", book.id))])
    kb.append([
        BTN(f"📝 Notlar ({len(book.notes)})", _cb("notes", book.id, 0)),
        BTN(f"📑 Oturumlar ({len(book.sessions)})", _cb("sessions", book.id)),
    ])
    kb.append([
        BTN("❓ Soru Sor", _cb("question", "ask", book.id)),
        BTN("✏️ Düzenle", _cb("book_edit", book.id)),
    ])
    if book.status != BookStatus.FINISHED:
        kb.append([BTN("🏁 Kitabı Bitirdim", _cb("finish", "start", book.id))])
    kb.append([BTN("🗑️ Kitabı Sil", _cb("book_del", "ask", book.id))])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb), book.cover_url


async def screen_recap(book_id: int) -> ScreenResult:
    book = await data.get_book(book_id)
    if not book:
        return "Kitap bulunamadı.", KB([_nav_row(False)])
    summaries = await data.summaries_for_book(book_id)
    sessions = await data.list_sessions_for_book(book_id)
    last_finished = next((s for s in sessions if s.ended_at is not None), None)

    lines = [f"🏠 Ana › ▶️ Okumaya Başla › <b>{esc(book.title)}</b>", ""]
    if summaries:
        lines.append("Geçen kaldığın yerden özetler:\n")
        for s in summaries[-3:]:
            ts = to_local(s.created_at).strftime("%d %b") if s.created_at else ""
            lines += [f"📋 s.{s.page or '—'}  ({ts})",
                      f"<i>“{esc(s.transcript[:300])}”</i>", ""]
    else:
        lines += ["Bu kitap için henüz Özet notun yok.", ""]

    suggested = (last_finished.end_page if last_finished and last_finished.end_page
                 else (book.read_pages or 1))
    lines.append(f"Şimdi hangi sayfadan başlıyorsun? Son bilinen: s.{suggested}")
    return "\n".join(lines), KB([
        [BTN(f"▶️ s.{suggested} — Başla", _cb("begin", book_id, suggested))],
        [BTN("✏️ Manuel sayfa gir", _cb("begin_manual", book_id))],
        _nav_row(),
    ])


async def screen_open_sessions() -> ScreenResult:
    sessions = await data.list_open_sessions()
    if not sessions:
        return ("🏠 Ana › 🟢 <b>Açık Oturumlar</b>\n\n(Şu an açık oturum yok.)",
                KB([_nav_row()]))
    lines = [f"🏠 Ana › 🟢 <b>Açık Oturumlar</b>  ({len(sessions)})", ""]
    kb = []
    for sess in sessions:
        book = await data.get_book(sess.book_id)
        title = book.title if book else "?"
        started = to_local(sess.started_at)
        elapsed = int((datetime.now(started.tzinfo) - started).total_seconds() / 60) if started else 0
        lines.append(
            f"• <code>{esc(sess.code)}</code> · <b>{esc(title)}</b> · {elapsed} dk · {len(sess.notes)} not"
        )
        kb.append([BTN(f"🟢 {sess.code} · {title}", _cb("session_open", sess.id))])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


async def screen_active_session(session: Session) -> ScreenResult:
    book = await data.get_book(session.book_id)
    title = book.title if book else "?"
    started = to_local(session.started_at)
    elapsed = max(1, int((datetime.now(started.tzinfo) - started).total_seconds() / 60)) if started else 1
    text = (
        f"🏠 Ana › 🟢 Okuyor: <b>{esc(title)}</b>  <code>{esc(session.code)}</code>\n\n"
        f"📖 <b>{esc(title)}</b>\n"
        f"🟢 Oturum aktif — {elapsed} dk\n"
        f"📄 Başlangıç sayfası: s.{session.start_page or '?'}\n"
        f"📝 Bu oturumda: {len(session.notes)} not\n\n"
        "🎤 Sesli mesaj at → not eklenir\n"
        "📷 Foto at → sayfa OCR'lanır (sayfa no varsa otomatik)\n"
        "✍️ Düz metin at → düz not eklenir"
    )
    kb = [
        [BTN("❓ Soru Sor (Gemini)", _cb("question", "ask", session.book_id))],
        [BTN(f"📝 Bu Oturumun Notları ({len(session.notes)})", _cb("session_notes", session.id))],
        [BTN("⏸️ Duraklat", _cb("pause", session.id)),
         BTN("⏹️ Bitir", _cb("end", session.id))],
        _nav_row(),
    ]
    return text, KB(kb)


def screen_note_confirm(draft: dict[str, Any]) -> ScreenResult:
    cats = [Category.QUOTE, Category.IDEA, Category.NEW_INFO,
            Category.WORD, Category.CONCEPT, Category.SUMMARY]
    current = draft["category"]
    short = draft.get("book_short_code") or ""
    title = draft.get("book_title") or ""
    parts = [
        f"🏠 Ana › 🟢 Okuyor › 📝 <b>Yeni Not</b>  ·  <code>{esc(short)}</code> {esc(title)}",
        "", "✅ Transkript:", f"<i>“{esc(draft['transcript'])}”</i>", "",
        f"🤖 Kategori: <b>{esc(current)}</b>",
        f"📄 Sayfa: s.{draft.get('page') or '—'}",
    ]
    if draft.get("definition"):
        parts += ["", f"📚 Otomatik tanım: {esc(draft['definition'])}"]
    if draft.get("explanation"):
        parts += ["", f"💡 Gemini açıklaması: {esc(draft['explanation'])}"]

    def _cat_btn(c):
        return BTN(("✓ " if c.value == current else "") + c.value, _cb("voice", "cat", c.value))

    kb = [
        [_cat_btn(c) for c in cats[:3]],
        [_cat_btn(c) for c in cats[3:]],
        [BTN(f"📄 s.{draft.get('page') or '—'}", "voice:page"),
         BTN("✏️ Transkripti düzelt", "voice:edit")],
        [BTN("💡 Açıkla (Gemini)", "voice:explain")],
        [BTN("✅ Kaydet", "voice:save"), BTN("❌ İptal", "voice:cancel")],
    ]
    return "\n".join(parts), KB(kb)


def screen_question_idle(book: data.Book) -> ScreenResult:
    text = (
        f"🏠 Ana › ❓ <b>Soru Modu</b>  ·  {esc(book.title)}\n\n"
        "Gemini bu kitabın notlarına bakarak cevap verecek.\n\n"
        "Sorunu yaz ya da sesli at."
    )
    return text, KB([[BTN("⬅️ Vazgeç", _cb("book", book.id))]])


def screen_question_answer(book: data.Book, question: str, answer: str) -> ScreenResult:
    text = (
        f"🏠 Ana › ❓ <b>Soru › Cevap</b>  ·  {esc(book.title)}\n\n"
        f"❓ <i>{esc(question)}</i>\n\n🤖 {esc(answer)}"
    )
    return text, KB([
        [BTN("📝 Not olarak kaydet", _cb("question", "save", book.id))],
        [BTN("🔁 Yeni soru", _cb("question", "ask", book.id))],
        [BTN("📖 Kitaba dön", _cb("book", book.id))],
    ])


def screen_end_session_summary_prompt(session_id: int) -> ScreenResult:
    text = (
        "🏠 Ana › ⏹️ <b>Bitir › 📋 Özet</b>\n\n"
        "Son adım: bu oturumu kısaca özetler misin?\n\n"
        "Sesli mesaj at — Özet kategorili bir not olarak kaydederim. "
        "Bir sonraki oturum başında sana geri okuyacağım."
    )
    return text, KB([[BTN("⏭️ Özeti atla", _cb("end_done", session_id))]])


def screen_export_menu() -> ScreenResult:
    text = (
        "🏠 Ana › 📤 <b>Veriyi Dışa Aktar</b>\n\n"
        "<b>Hangisi ne işe yarar?</b>\n"
        "• <b>PDF</b> — tek kitabın okuma günlüğü (paylaşılabilir).\n"
        "• <b>CSV</b> — Excel'de açılır not tablosu.\n"
        "• <b>Markdown</b> — Obsidian / NotebookLM için.\n"
        "• <b>Tüm veri JSON</b> — yedek (tekrar yüklenebilir).\n"
        "• <b>Tüm kitaplar ZIP</b> — her kitap için PDF + JSON + MD."
    )
    return text, KB([
        [BTN("📕 Kitap PDF", "export:choose:pdf")],
        [BTN("📊 Tek kitap CSV", "export:choose:csv")],
        [BTN("📝 Tek kitap Markdown", "export:choose:md")],
        [BTN("📦 Tüm veri JSON", "export:all:json")],
        [BTN("🗂️ Tüm kitaplar ZIP", "export:all:zip")],
        _nav_row(),
    ])


async def screen_settings() -> ScreenResult:
    s = await data.get_settings()
    on, off = "✅", "❌"
    text = (
        "🏠 Ana › ⚙️ <b>Ayarlar</b>\n\n"
        f"{on if s.nudge_enabled else off} Proaktif hatırlatma\n"
        f"   Sıklık: her <b>{s.nudge_interval_days}</b> günde bir kontrol\n\n"
        f"{on if s.auto_categorize else off} Otomatik kategori önerisi\n"
        f"{on if s.auto_explain else off} Notları otomatik açıkla (Gemini)\n"
        f"{on if s.summary_prompt_on_end else off} Oturum bitince özet sor"
    )
    return text, KB([
        [BTN(
            "🔕 Hatırlatmayı kapat" if s.nudge_enabled else "🔔 Hatırlatmayı aç",
            "settings:toggle:nudge_enabled",
        )],
        [BTN("− gün", "settings:nudge_interval:-1"),
         BTN(f"{s.nudge_interval_days} gün", "noop"),
         BTN("+ gün", "settings:nudge_interval:+1")],
        [BTN("❌ Otomatik kategori kapalı" if not s.auto_categorize else "✅ Otomatik kategori açık",
             "settings:toggle:auto_categorize")],
        [BTN("❌ Otomatik açıklama kapalı" if not s.auto_explain else "✅ Otomatik açıklama açık",
             "settings:toggle:auto_explain")],
        [BTN("❌ Özet sorma kapalı" if not s.summary_prompt_on_end else "✅ Özet sorma açık",
             "settings:toggle:summary_prompt_on_end")],
        _nav_row(),
    ])


async def screen_notes_for_book(book_id: int, offset: int = 0) -> ScreenResult:
    """Paginated note list (25 per page)."""
    PAGE = 25
    notes, total = await data.notes_for_book(book_id, offset=offset, limit=PAGE)
    book = await data.get_book(book_id)
    title = book.title if book else "?"
    if total == 0:
        return (
            f"🏠 Ana › 📚 {esc(title)} › 📝 <b>Notlar</b>\n\n(Henüz not yok.)",
            KB([_nav_row()]),
        )
    page_num = (offset // PAGE) + 1
    total_pages = (total + PAGE - 1) // PAGE
    lines = [
        f"🏠 Ana › 📚 {esc(title)} › 📝 <b>Notlar</b>",
        f"Sayfa {page_num}/{total_pages} · Toplam {total} not\n",
    ]
    kb = []
    for n in notes:
        snip = n.transcript[:60] + ("…" if len(n.transcript) > 60 else "")
        lines += [
            f"<code>{esc(n.code)}</code> [{esc(n.category.value)}] s.{n.page or '—'}",
            f"  <i>{esc(snip)}</i>",
        ]
        kb.append([BTN(f"{n.code} · {n.category.value}", _cb("note", n.id))])
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(BTN("⬅️ Önceki", _cb("notes", book_id, max(0, offset - PAGE))))
    if offset + PAGE < total:
        nav.append(BTN("Sonraki ➡️", _cb("notes", book_id, offset + PAGE)))
    if nav:
        kb.append(nav)
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


async def screen_note_detail(note_id: int) -> ScreenResult:
    note = await data.get_note(note_id)
    if not note:
        return "Not bulunamadı.", KB([_nav_row()])
    book = await data.get_book(note.book_id)
    title = book.title if book else "?"
    ts = to_local(note.created_at).strftime("%d %b %Y · %H:%M") if note.created_at else ""
    lines = [
        f"🏠 Ana › 📚 {esc(title)} › 📝 <b>Not</b>  ·  <code>{esc(note.code)}</code>",
        "",
        f"📅 {esc(ts)}",
        f"🏷️ {esc(note.category.value)}  ·  📄 s.{note.page or '—'}",
        "",
        f"<i>“{esc(note.transcript)}”</i>",
    ]
    if note.definition:
        lines += ["", f"📚 Tanım: {esc(note.definition)}"]
    if note.explanation:
        lines += ["", f"💡 Açıklama: {esc(note.explanation)}"]
    if note.is_favorite:
        lines.append("\n⭐ Favori")
    return "\n".join(lines), KB([
        [BTN("⭐ Favoriden çıkar" if note.is_favorite else "⭐ Favoriye ekle",
             _cb("note_fav", note.id))],
        [BTN("✏️ Transkripti düzelt", _cb("note_edit", note.id)),
         BTN("📄 Sayfa değiştir", _cb("note_page", note.id))],
        [BTN("🗑️ Notu sil", _cb("note_del", note.id))],
        _nav_row(),
    ])


async def screen_sessions_for_book(book_id: int) -> ScreenResult:
    sessions = await data.list_sessions_for_book(book_id)
    book = await data.get_book(book_id)
    title = book.title if book else "?"
    if not sessions:
        return (
            f"🏠 Ana › 📚 {esc(title)} › 📑 <b>Oturumlar</b>\n\n(Henüz oturum yok.)",
            KB([_nav_row()]),
        )
    lines = [
        f"🏠 Ana › 📚 {esc(title)} › 📑 <b>Oturumlar</b>",
        f"Toplam {len(sessions)} oturum\n",
    ]
    kb = []
    for sess in sessions:
        started = to_local(sess.started_at)
        date_str = started.strftime("%d %b %H:%M") if started else "?"
        dur = f"{sess.duration_min} dk" if sess.duration_min else "açık"
        pages = ""
        if sess.start_page is not None:
            pages = f" · s.{sess.start_page}"
            if sess.end_page and sess.end_page != sess.start_page:
                pages += f"→{sess.end_page}"
        icon = "🟢" if sess.ended_at is None else "✅"
        lines.append(
            f"{icon} <code>{esc(sess.code)}</code> · {esc(date_str)} · "
            f"{dur}{pages} · {len(sess.notes)} not"
        )
        kb.append([BTN(f"{sess.code} · {date_str}", _cb("session_open", sess.id))])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


async def screen_session_notes(session_id: int) -> ScreenResult:
    notes = await data.notes_for_session(session_id)
    sess = await data.get_session(session_id)
    if sess is None:
        return "Oturum bulunamadı.", KB([_nav_row()])
    book = await data.get_book(sess.book_id)
    title = book.title if book else "?"
    lines = [
        f"🏠 Ana › 📑 <code>{esc(sess.code)}</code> · {esc(title)} · <b>Notlar</b>",
        f"Toplam {len(notes)} not\n",
    ]
    kb = []
    if not notes:
        lines.append("<i>(Bu oturumda not yok.)</i>")
    for n in notes:
        snip = n.transcript[:60] + ("…" if len(n.transcript) > 60 else "")
        lines += [
            f"<code>{esc(n.code)}</code> [{esc(n.category.value)}] s.{n.page or '—'}",
            f"  <i>{esc(snip)}</i>",
        ]
        kb.append([BTN(f"{n.code} · {n.category.value}", _cb("note", n.id))])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


async def screen_search_results(query: str) -> ScreenResult:
    results = await data.search_notes(query)
    lines = [f"🏠 Ana › 🔍 <b>Ara</b>: <code>{esc(query)}</code>", ""]
    kb = []
    if not results:
        lines.append("<i>(Sonuç yok.)</i>")
    else:
        lines.append(f"{len(results)} sonuç:\n")
        for note, book in results[:20]:
            snip = note.transcript[:80] + ("…" if len(note.transcript) > 80 else "")
            lines.append(
                f"<code>{esc(note.code)}</code> · {esc(book.title)}\n  <i>{esc(snip)}</i>"
            )
            kb.append([BTN(f"{note.code} · {note.category.value}", _cb("note", note.id))])
    kb.append([BTN("🔁 Yeni arama", "search:start")])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


async def screen_glossary() -> ScreenResult:
    entries = await data.list_glossary()
    lines = [f"🏠 Ana › 📖 <b>Sözlük</b>  ({len(entries)} terim)", ""]
    if not entries:
        lines.append("<i>(Henüz Kelime/Kavram notun yok.)</i>")
        return "\n".join(lines), KB([_nav_row()])
    kb = []
    for note, book in entries[:50]:
        marker = "🔤" if note.category == Category.WORD else "🧠"
        term = note.transcript[:40]
        lines.append(f"{marker} <b>{esc(term)}</b>  ·  <i>{esc(book.short_code)}</i>")
        if note.definition:
            lines.append(f"   {esc(note.definition[:120])}")
        kb.append([BTN(f"{marker} {term[:30]}", _cb("note", note.id))])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


async def screen_quotes(favorites_only: bool = False) -> ScreenResult:
    entries = await data.list_quotes(favorites_only=favorites_only)
    title = "💬 Favori Alıntılar" if favorites_only else "💬 Tüm Alıntılar"
    lines = [f"🏠 Ana › <b>{title}</b>  ({len(entries)})", ""]
    if not entries:
        lines.append("<i>(Henüz alıntı yok.)</i>")
        return "\n".join(lines), KB([_nav_row()])
    kb = []
    for note, book in entries[:25]:
        star = "⭐ " if note.is_favorite else ""
        lines.append(
            f"{star}<code>{esc(note.code)}</code> · {esc(book.title)}\n"
            f"  <i>“{esc(note.transcript[:140])}”</i>"
        )
        kb.append([BTN(f"{star}{note.code} · {book.short_code}", _cb("note", note.id))])
    kb.append([BTN(
        "🌟 Sadece favoriler" if not favorites_only else "📜 Tümü",
        "quotes:fav" if not favorites_only else "quotes:all",
    )])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


async def screen_stats() -> ScreenResult:
    s_all = await data.compute_stats(period_days=None)
    s_month = await data.compute_stats(period_days=30)
    lines = [
        "🏠 Ana › 📊 <b>İstatistik</b>", "",
        f"📚 Toplam kitap: <b>{s_all['total_books']}</b>",
        f"✅ Bitirilen: <b>{s_all['finished_count']}</b>  ·  "
        f"📖 Okuyor: <b>{s_all['reading_count']}</b>  ·  "
        f"⏸️ Durmuş: <b>{s_all['paused_count']}</b>",
        f"📝 Toplam not: <b>{s_all['note_count']}</b>",
        f"📑 Toplam oturum: <b>{s_all['session_count']}</b>",
        f"⏱️ Toplam okuma: <b>{s_all['total_hours']} sa {s_all['total_min_rem']} dk</b>",
        f"🔥 Streak: <b>{s_all['streak_days']} gün</b>", "",
        "<b>━━━ Son 30 gün ━━━</b>",
        f"📑 Oturum: <b>{s_month['session_count']}</b>  ·  "
        f"⏱️ <b>{s_month['total_hours']} sa {s_month['total_min_rem']} dk</b>",
    ]
    if s_month["finished_in_period"]:
        lines += ["", "<b>Bu ay bitirilenler:</b>"]
        for fb in s_month["finished_in_period"]:
            pg = f" · {fb['total_pages']} sayfa" if fb.get("total_pages") else ""
            rt = f" · {'⭐'*fb['rating']}" if fb.get("rating") else ""
            lines.append(f"  • {esc(fb['title'])}{pg}{rt}")
    best_label, best_min = s_month["best_bucket"]
    if best_min:
        lines += ["", f"⏰ <b>En verimli zaman aralığı:</b> {esc(best_label)} ({best_min} dk)"]
        examples = s_month["bucket_sessions"].get(best_label, [])[:5]
        if examples:
            lines.append("<i>Bu aralığa giren oturumlar:</i>")
            for ex in examples:
                started: datetime = ex["started_at"]
                lines.append(
                    f"  • <code>{esc(ex['code'])}</code> · {esc(ex['book_title'])} · "
                    f"{started.strftime('%d %b %H:%M')} · {ex['duration_min']} dk"
                )
    return "\n".join(lines), KB([
        [BTN("📤 ZIP arşivi (PDF+JSON+MD)", "export:all:zip")],
        _nav_row(),
    ])


# ─── Finish ritual ───

def screen_finish_rating(book: data.Book) -> ScreenResult:
    text = f"🏁 <b>{esc(book.title)}</b> bitirildi!\n\nÖnce: <b>kaç yıldız verirsin?</b>"
    return text, KB([
        [BTN("⭐",       _cb("finish", "rate", book.id, 1)),
         BTN("⭐⭐",      _cb("finish", "rate", book.id, 2)),
         BTN("⭐⭐⭐",     _cb("finish", "rate", book.id, 3))],
        [BTN("⭐⭐⭐⭐",    _cb("finish", "rate", book.id, 4)),
         BTN("⭐⭐⭐⭐⭐",   _cb("finish", "rate", book.id, 5))],
        [BTN("⏭️ Atla", _cb("finish", "skip_rate", book.id))],
    ])


def screen_finish_review_prompt(book: data.Book) -> ScreenResult:
    text = (
        f"📝 <b>{esc(book.title)}</b> — bir cümleyle nasıldı?\n\n"
        "Yaz ya da sesli at. (İstersen atla.)"
    )
    return text, KB([[BTN("⏭️ Atla", _cb("finish", "skip_review", book.id))]])


async def screen_finish_favorites(book: data.Book) -> ScreenResult:
    quotes = await data.quotes_for_book(book.id)
    lines = [f"⭐ <b>{esc(book.title)}</b> — favori 3 alıntını seç", ""]
    if not quotes:
        lines.append("<i>(Bu kitapta Alıntı kategorili not yok — bu adımı atlıyoruz.)</i>")
        return "\n".join(lines), KB([[BTN("Devam →", _cb("finish", "to_recommend", book.id))]])
    kb = []
    for q in quotes[:15]:
        marker = "⭐ " if q.is_favorite else ""
        snip = q.transcript[:60] + ("…" if len(q.transcript) > 60 else "")
        kb.append([BTN(f"{marker}{snip}", _cb("finish", "toggle_fav", book.id, q.id))])
    kb.append([BTN("Devam →", _cb("finish", "to_recommend", book.id))])
    return "\n".join(lines), KB(kb)


def screen_finish_recommend(book: data.Book) -> ScreenResult:
    text = f"👍 <b>{esc(book.title)}</b> — başkasına önerir misin?"
    return text, KB([[
        BTN("👍 Evet",  _cb("finish", "rec", book.id, "yes")),
        BTN("👎 Hayır", _cb("finish", "rec", book.id, "no")),
        BTN("🤷 Bilmem", _cb("finish", "rec", book.id, "skip")),
    ]])


# ─── New book ───

def screen_newbook_method() -> ScreenResult:
    return "🏠 Ana › ➕ <b>Yeni Kitap</b>\n\nYeni kitabı nasıl ekleyelim?", KB([
        [BTN("🔢 ISBN ile (kapak + metadata otomatik)", "newbook:isbn")],
        [BTN("✍️ Elle (sadece başlık)", "newbook:manual")],
        _nav_row(),
    ])


def screen_newbook_preview(draft: dict[str, Any]) -> ScreenResult:
    lines = [
        "🏠 Ana › ➕ <b>Yeni Kitap</b> › Önizleme", "",
        "✅ Bulunan bilgiler:", "",
        f"📖 <b>{esc(draft.get('title') or '(başlık yok)')}</b>",
    ]
    if draft.get("author"):
        lines.append(f"✍️ {esc(draft['author'])}")
    if draft.get("isbn"):
        lines.append(f"🌐 ISBN: {esc(draft['isbn'])}")
    if draft.get("genre"):
        sub = f" › {esc(draft['subgenre'])}" if draft.get("subgenre") else ""
        lines.append(f"🏷️ {esc(draft['genre'])}{sub}")
    if draft.get("total_pages"):
        lines.append(f"📄 {draft['total_pages']} sayfa")
    if draft.get("cover_url"):
        lines.append("🖼️ Kapak görseli bulundu")
    lines += ["", "Kaydedelim mi? İstersen önce 3 harflik kısa kod (örn. SVC) verebilirsin."]
    return "\n".join(lines), KB([
        [BTN("✅ Otomatik kod ile kaydet", "newbook:save")],
        [BTN("🔤 Kısa kodu ben gireyim", "newbook:codefirst")],
        [BTN("❌ İptal", "main")],
    ])


# ────────────────────────── /start command ──────────────────────────


@_safe_handler
async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    cid = _chat_id(update)
    logger.info("bot.command_start", user_id=user_id, chat_id=cid)
    if cid is not None:
        await _clear(cid, "awaiting", "draft_note", "mode", "question_book_id",
                     "newbook_draft", "last_qa", "finish_book_id", "pending_input")
    text, kb = await screen_main()
    await update.message.reply_text(
        text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


# ────────────────────────── input routing ──────────────────────────


async def _resolve_session_for_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Session | None:
    """Decide which open session a new note/text belongs to.

    Priority: most-recently-used session (chat_data) > only-open-session >
    multi-open: ask user. Returns None when prompted to choose (or when no
    open session exists; appropriate UI is already sent).
    """
    cid = _chat_id(update)
    default_sess_id = await _get(cid, "default_session") if cid else None
    if isinstance(default_sess_id, int):
        sess = await data.get_session(default_sess_id)
        if sess and sess.ended_at is None:
            return sess

    open_sessions = await data.list_open_sessions()
    if not open_sessions:
        if update.message:
            await update.message.reply_text(
                "🟡 Açık oturumun yok. Önce ▶️ Oturum Başlat de.",
                parse_mode=ParseMode.HTML,
            )
        return None
    if len(open_sessions) == 1:
        return open_sessions[0]

    msg = update.message
    if msg is None or cid is None:
        return None
    pending = {"kind": "voice" if msg.voice else "photo" if msg.photo else "text"}
    if msg.text:
        pending["text"] = msg.text
    await _set(cid, "pending_input", pending, ttl_s=900)
    kb_rows = []
    for sess in open_sessions:
        book = await data.get_book(sess.book_id)
        title = book.title if book else "?"
        kb_rows.append([BTN(f"{sess.code} · {title}", _cb("route_pending", sess.id))])
    kb_rows.append([BTN("❌ Vazgeç", "route_pending_cancel")])
    await msg.reply_text(
        "🟢 Birden fazla açık oturumun var. Bu not hangisi için?",
        reply_markup=KB(kb_rows), parse_mode=ParseMode.HTML,
    )
    return None


# ────────────────────────── voice / photo / text handlers ──────────────────────────


@_safe_handler
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = _chat_id(update)
    msg = update.message
    logger.info("bot.voice.received", user_id=update.effective_user.id,
                file_id=msg.voice.file_id, duration=msg.voice.duration)

    mode = await _get(cid, "mode") if cid else None
    if isinstance(mode, dict) and mode.get("name") == "question":
        await _handle_question_input(update, context, voice=True, book_id=mode.get("book_id"))
        return

    awaiting = await _get(cid, "awaiting") if cid else None
    if isinstance(awaiting, dict):
        if awaiting.get("type") == "end_summary":
            await _handle_end_summary_input(update, context, voice=True)
            return
        if awaiting.get("type") == "finish_review":
            await _handle_finish_review_input(update, context, voice=True)
            return

    session = await _resolve_session_for_input(update, context)
    if session is None:
        return
    await _process_voice_into_note(update, context, session)


async def _process_voice_into_note(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session
) -> None:
    msg = update.message
    cid = _chat_id(update)
    await msg.chat.send_action(ChatAction.TYPING)
    try:
        transcript = await _transcribe_voice_msg(msg)
        settings = await data.get_settings()
        category = await ai.suggest_category(transcript) if settings.auto_categorize else Category.NEW_INFO
    except Exception as e:
        logger.error("bot.voice.failed", error=str(e), exc_info=True)
        await _err_reply(msg, e, "Ses işlenirken hata")
        return
    definition = None
    if category in (Category.WORD, Category.CONCEPT):
        try:
            definition = await ai.define_term(transcript, category)
        except Exception as e:
            logger.warning("bot.voice.define_failed", error=str(e))
    book = await data.get_book(session.book_id)
    draft = _build_draft(session, book, transcript, category, session.start_page, definition)
    if cid is not None:
        await _set(cid, "draft_note", draft)
    text, kb = screen_note_confirm(draft)
    await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@_safe_handler
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = _chat_id(update)
    msg = update.message
    logger.info("bot.photo.received", user_id=update.effective_user.id)

    session = await _resolve_session_for_input(update, context)
    if session is None:
        return

    await msg.chat.send_action(ChatAction.TYPING)
    try:
        photo_file = await msg.photo[-1].get_file()
        image_bytes = await photo_file.download_as_bytearray()
        transcript, detected_page = await ai.ocr_image(bytes(image_bytes), "image/jpeg")
        settings = await data.get_settings()
        category = await ai.suggest_category(transcript) if settings.auto_categorize else Category.NEW_INFO
    except Exception as e:
        logger.error("bot.photo.failed", error=str(e), exc_info=True)
        await _err_reply(msg, e, "Fotoğraf işlenirken hata")
        return

    book = await data.get_book(session.book_id)
    draft = _build_draft(session, book, transcript, category, detected_page)
    if cid is not None:
        await _set(cid, "draft_note", draft)

    if detected_page is None:
        if cid is not None:
            await _set(cid, "awaiting", {"type": "draft_page"})
        text = (
            f"📷 <b>OCR tamam</b>  ·  <code>{esc(book.short_code if book else '')}</code> "
            f"{esc(book.title if book else '')}\n\n"
            "Sayfa numarasını fotoğrafta bulamadım. Lütfen sayfa numarasını yaz "
            "(sadece sayı). Sonra notu onaylarsın."
        )
        await msg.reply_text(
            text, reply_markup=KB([[BTN("📄 Sayfa belirsiz, atla", "voice:skippage")]]),
            parse_mode=ParseMode.HTML,
        )
        return

    text, kb = screen_note_confirm(draft)
    await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@_safe_handler
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = _chat_id(update)
    msg = update.message
    text = (msg.text or "").strip()
    logger.info("bot.text.received", user_id=update.effective_user.id, length=len(text))

    mode = await _get(cid, "mode") if cid else None
    if isinstance(mode, dict) and mode.get("name") == "question":
        await _handle_question_input(update, context, voice=False, book_id=mode.get("book_id"))
        return

    awaiting = await _get(cid, "awaiting") if cid else None
    if isinstance(awaiting, dict):
        kind = awaiting.get("type")
        if kind == "search":
            await _clear(cid, "awaiting")
            s_text, kb = await screen_search_results(text)
            await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        if kind == "end_summary":
            await _handle_end_summary_input(update, context, voice=False)
            return
        if kind == "finish_review":
            await _handle_finish_review_input(update, context, voice=False)
            return
        await _handle_awaiting_input(update, context, text, awaiting)
        return

    session = await _resolve_session_for_input(update, context)
    if session is None:
        return
    await _process_text_into_note(update, context, session, text)


async def _process_text_into_note(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    session: Session, text: str,
) -> None:
    msg = update.message
    cid = _chat_id(update)
    settings = await data.get_settings()
    try:
        if settings.auto_categorize:
            category = await ai.suggest_category(text)
            definition = await ai.define_term(text, category)
        else:
            category, definition = Category.NEW_INFO, None
    except Exception as e:
        logger.error("bot.text.categorize_failed", error=str(e), exc_info=True)
        category, definition = Category.NEW_INFO, None
    book = await data.get_book(session.book_id)
    draft = _build_draft(session, book, text, category, session.start_page, definition)
    if cid is not None:
        await _set(cid, "draft_note", draft)
    s_text, kb = screen_note_confirm(draft)
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ────────────────────────── input sub-routines ──────────────────────────


async def _handle_question_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    voice: bool, book_id: int | None,
) -> None:
    msg = update.message
    cid = _chat_id(update)
    if voice:
        await msg.chat.send_action(ChatAction.TYPING)
        try:
            question = await _transcribe_voice_msg(msg)
        except Exception as e:
            logger.error("bot.question.transcribe_failed", error=str(e), exc_info=True)
            await _err_reply(msg, e, "Soru transkript edilemedi")
            return
    else:
        question = (msg.text or "").strip()

    if book_id is None:
        await msg.reply_text("Soru için önce bir kitap seçmen lazım.")
        if cid is not None:
            await _clear(cid, "mode")
        return
    book = await data.get_book(book_id)
    notes_paginated, _ = await data.notes_for_book(book_id, offset=0, limit=200)

    await msg.chat.send_action(ChatAction.TYPING)
    try:
        answer = await ai.answer_question(question, book, notes_paginated)
    except Exception as e:
        logger.error("bot.question.answer_failed", error=str(e), exc_info=True)
        await _err_reply(msg, e, "Soru cevaplanamadı")
        return

    if cid is not None:
        await _set(cid, "last_qa", {"question": question, "answer": answer, "book_id": book_id})
        await _clear(cid, "mode")
    text, kb = screen_question_answer(book, question, answer)
    await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _handle_end_summary_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, voice: bool
) -> None:
    msg = update.message
    cid = _chat_id(update)
    awaiting = (await _get(cid, "awaiting")) if cid else None
    if not isinstance(awaiting, dict):
        return
    session_id = awaiting.get("session_id")
    book_id = awaiting.get("book_id")
    end_page = awaiting.get("end_page", 0)

    if voice:
        await msg.chat.send_action(ChatAction.TYPING)
        try:
            summary_text = await _transcribe_voice_msg(msg)
        except Exception as e:
            logger.error("bot.end_summary.transcribe_failed", error=str(e), exc_info=True)
            await _err_reply(msg, e, "Özet transkript edilemedi")
            return
    else:
        summary_text = (msg.text or "").strip()

    if not (session_id and book_id):
        await msg.reply_text("❌ Oturum bilgisi kayıp. /start ile yeniden başlat.")
        return

    try:
        await data.add_note(
            book_id=book_id, session_id=session_id,
            category=Category.SUMMARY, page=end_page, transcript=summary_text,
        )
        await data.end_session(session_id, end_page)
    except Exception as e:
        logger.error("bot.end_summary.save_failed", error=str(e), exc_info=True)
        await _err_reply(msg, e, "Kayıt sırasında hata")
        return

    if cid is not None:
        await _clear(cid, "awaiting")
    await msg.reply_text("✅ Özet kaydedildi, oturum kapandı. İyi okumalar!")
    text, kb = await screen_main()
    await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _handle_finish_review_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, voice: bool
) -> None:
    msg = update.message
    cid = _chat_id(update)
    awaiting = (await _get(cid, "awaiting")) if cid else None
    if not isinstance(awaiting, dict):
        return
    book_id = awaiting.get("book_id")
    if not book_id:
        return
    if voice:
        await msg.chat.send_action(ChatAction.TYPING)
        try:
            review = await _transcribe_voice_msg(msg)
        except Exception as e:
            await _err_reply(msg, e, "Yorum transkript edilemedi")
            return
    else:
        review = (msg.text or "").strip()
    await data.update_book(book_id, one_line_review=review)
    if cid is not None:
        await _clear(cid, "awaiting")
    book = await data.get_book(book_id)
    text, kb = await screen_finish_favorites(book)
    await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ────────────────────────── awaiting-input dispatcher ──────────────────────────
#
# Text the user types in response to a prompt. Each `kind` of awaiting state
# has its own small handler.

async def _await_page_to_begin(update, context, text, awaiting):
    msg = update.message
    cid = _chat_id(update)
    try:
        page = int(text)
    except ValueError:
        await msg.reply_text("Geçerli bir sayı gir (örn: 47).")
        return
    session = await data.start_session(awaiting["book_id"], page)
    if cid is not None:
        await _clear(cid, "awaiting")
    s_text, kb = await screen_active_session(session)
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_end_page(update, context, text, awaiting):
    msg = update.message
    cid = _chat_id(update)
    try:
        page = int(text)
    except ValueError:
        await msg.reply_text("Geçerli bir sayı gir.")
        return
    if cid is None:
        return
    settings = await data.get_settings()
    if not settings.summary_prompt_on_end:
        await data.end_session(awaiting["session_id"], page)
        await _clear(cid, "awaiting")
        await msg.reply_text("✅ Oturum kapatıldı.")
        m_text, m_kb = await screen_main()
        await msg.reply_text(m_text, reply_markup=m_kb, parse_mode=ParseMode.HTML)
        return
    await _set(cid, "awaiting", {
        "type": "end_summary", "session_id": awaiting["session_id"],
        "book_id": awaiting["book_id"], "end_page": page,
    })
    s_text, kb = screen_end_session_summary_prompt(awaiting["session_id"])
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_draft_page(update, context, text, awaiting):
    msg = update.message
    cid = _chat_id(update)
    try:
        page = int(text)
    except ValueError:
        await msg.reply_text("Geçerli bir sayı gir.")
        return
    draft = await _get(cid, "draft_note") if cid else None
    if isinstance(draft, dict):
        draft["page"] = page
        if cid is not None:
            await _set(cid, "draft_note", draft)
            await _clear(cid, "awaiting")
        s_text, kb = screen_note_confirm(draft)
        await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_draft_transcript(update, context, text, awaiting):
    msg = update.message
    cid = _chat_id(update)
    draft = await _get(cid, "draft_note") if cid else None
    if isinstance(draft, dict):
        draft["transcript"] = text
        if cid is not None:
            await _set(cid, "draft_note", draft)
            await _clear(cid, "awaiting")
        s_text, kb = screen_note_confirm(draft)
        await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_newbook_title(update, context, text, awaiting):
    msg = update.message
    cid = _chat_id(update)
    if not text:
        await msg.reply_text("Boş başlık olmaz. Tekrar yaz:")
        return
    book = await data.create_book(title=text)
    if cid is not None:
        await _clear(cid, "awaiting")
    await _show_onboarding_or_book(update, context, book)


async def _await_newbook_short_code(update, context, text, awaiting):
    msg = update.message
    cid = _chat_id(update)
    norm = data.normalize_short_code(text)
    if not norm:
        await msg.reply_text("⚠️ Geçersiz kod. 2-8 büyük harf/rakam olmalı. Tekrar dene ya da /start yaz.")
        return
    pending = awaiting.get("pending_book", {})
    pending["short_code"] = norm
    book = await data.create_book(**pending)
    if cid is not None:
        await _clear(cid, "awaiting")
    await _show_onboarding_or_book(update, context, book)


async def _await_newbook_isbn(update, context, text, awaiting):
    msg = update.message
    cid = _chat_id(update)
    isbn = text
    await msg.chat.send_action(ChatAction.TYPING)
    try:
        metadata = await data.lookup_book_metadata(isbn)
    except Exception as e:
        await _err_reply(msg, e, "Aranamadı")
        return
    if metadata is None:
        await msg.reply_text(
            f"⚠️ ISBN <code>{esc(isbn)}</code> için kitap bulunamadı.\n\n"
            "ISBN'i kontrol edip tekrar yaz, ya da elle eklemek için "
            "Ana Menü → Yeni Kitap → Elle seçeneğini kullan.",
            parse_mode=ParseMode.HTML,
        )
        return
    if cid is not None:
        await _set(cid, "newbook_draft", metadata)
        await _clear(cid, "awaiting")
    s_text, kb = screen_newbook_preview(metadata)
    if metadata.get("cover_url"):
        try:
            await msg.reply_photo(
                photo=metadata["cover_url"], caption=s_text,
                reply_markup=kb, parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            logger.warning("bot.newbook_isbn.cover_send_failed", error=str(e))
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_book_edit_field(update, context, text, awaiting):
    msg = update.message
    cid = _chat_id(update)
    book_id = awaiting["book_id"]
    field = awaiting["field"]
    if field == "tags":
        await data.update_book(book_id, tags=[t.strip() for t in text.split(",") if t.strip()])
    elif field == "personal_note":
        await data.update_book(book_id, personal_note=text)
    elif field == "short_code":
        norm = data.normalize_short_code(text)
        if not norm:
            await msg.reply_text("⚠️ Geçersiz kod (2-8 alfanumerik).")
            return
        await data.update_book(book_id, short_code=norm)
    elif field == "total_pages":
        try:
            pages = int(text)
        except ValueError:
            await msg.reply_text("Geçerli bir sayı gir.")
            return
        await data.update_book(book_id, total_pages=pages)
    if cid is not None:
        await _clear(cid, "awaiting")
    s_text, kb, cover = await screen_book_detail(book_id)
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_note_edit_field(update, context, text, awaiting):
    msg = update.message
    cid = _chat_id(update)
    note_id = awaiting["note_id"]
    field = awaiting["field"]
    if field == "transcript":
        await data.update_note(note_id, transcript=text)
    elif field == "page":
        try:
            page = int(text)
        except ValueError:
            await msg.reply_text("Geçerli bir sayı gir.")
            return
        await data.update_note(note_id, page=page)
    if cid is not None:
        await _clear(cid, "awaiting")
    s_text, kb = await screen_note_detail(note_id)
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


_AWAITING_HANDLERS = {
    "page_to_begin":      _await_page_to_begin,
    "end_page":           _await_end_page,
    "draft_page":         _await_draft_page,
    "draft_transcript":   _await_draft_transcript,
    "newbook_title":      _await_newbook_title,
    "newbook_short_code": _await_newbook_short_code,
    "newbook_isbn":       _await_newbook_isbn,
    "book_edit_field":    _await_book_edit_field,
    "note_edit_field":    _await_note_edit_field,
}


async def _handle_awaiting_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    text: str, awaiting: dict[str, Any],
) -> None:
    kind = awaiting.get("type")
    handler = _AWAITING_HANDLERS.get(kind)
    if handler is None:
        cid = _chat_id(update)
        logger.warning("bot.awaiting.unknown_kind", kind=kind)
        if cid is not None:
            await _clear(cid, "awaiting")
        await update.message.reply_text("Anlamadım. Ana menüye dön → /start")
        return
    await handler(update, context, text, awaiting)


async def _show_onboarding_or_book(
    update: Update, context: ContextTypes.DEFAULT_TYPE, book: data.Book
) -> None:
    """First book → onboarding tour; otherwise book detail."""
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    books = await data.list_books()
    if len(books) == 1:
        text, kb = screen_onboarding_after_first_book(book)
        await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    s_text, kb, cover = await screen_book_detail(book.id)
    if cover:
        await msg.reply_photo(photo=cover, caption=s_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ════════════════════════════════════════════════════════════════════════
# Callback dispatch
#
# Single dict-of-handlers keyed by the first ':'-segment of `callback_data`.
# Each handler takes `(update, context, args)` where `args` is the rest of
# the callback parts (split by ':'). Sub-actions inside a namespace are kept
# inline because they're short.
# ════════════════════════════════════════════════════════════════════════


@_safe_handler
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data_str = query.data or ""
    parts = data_str.split(":")
    head, args = parts[0], parts[1:]
    logger.info("bot.callback",
                user_id=update.effective_user.id if update.effective_user else None,
                callback=data_str)
    handler = _CALLBACKS.get(head)
    if handler is None:
        logger.warning("bot.callback.unknown", head=head)
        await query.answer(f"Bilinmeyen eylem: {head}", show_alert=True)
        return
    await handler(update, context, args)


# ── individual callback handlers ──

async def _cb_noop(update, context, args):
    await update.callback_query.answer()


async def _cb_main(update, context, args):
    text, kb = await screen_main()
    await _send_screen(update, context, text, kb)


async def _cb_books(update, context, args):
    text, kb = await screen_book_list()
    await _send_screen(update, context, text, kb)


async def _cb_book(update, context, args):
    text, kb, cover = await screen_book_detail(int(args[0]))
    await _send_screen(update, context, text, kb, photo_url=cover)


async def _cb_notes(update, context, args):
    offset = int(args[1]) if len(args) > 1 else 0
    text, kb = await screen_notes_for_book(int(args[0]), offset=offset)
    await _send_screen(update, context, text, kb)


async def _cb_note(update, context, args):
    text, kb = await screen_note_detail(int(args[0]))
    await _send_screen(update, context, text, kb)


async def _cb_note_fav(update, context, args):
    note_id = int(args[0])
    n = await data.get_note(note_id)
    if n:
        await data.update_note(note_id, is_favorite=not n.is_favorite)
    text, kb = await screen_note_detail(note_id)
    await _send_screen(update, context, text, kb)


async def _cb_note_edit(update, context, args):
    await _prompt(update, context, "✏️ Yeni transkripti yaz ve gönder.",
                  {"type": "note_edit_field", "note_id": int(args[0]), "field": "transcript"})


async def _cb_note_page(update, context, args):
    await _prompt(update, context, "📄 Sayfa numarasını yaz (sadece sayı).",
                  {"type": "note_edit_field", "note_id": int(args[0]), "field": "page"})


async def _cb_note_del(update, context, args):
    query = update.callback_query
    note_id = int(args[0])
    n = await data.get_note(note_id)
    if not n:
        await query.answer("Not bulunamadı.", show_alert=True)
        return
    book_id = n.book_id
    await data.delete_note(note_id)
    await query.answer("🗑️ Not silindi")
    text, kb = await screen_notes_for_book(book_id, offset=0)
    await _send_screen(update, context, text, kb)


async def _cb_sessions(update, context, args):
    text, kb = await screen_sessions_for_book(int(args[0]))
    await _send_screen(update, context, text, kb)


async def _cb_session_open(update, context, args):
    query = update.callback_query
    sess = await data.get_session(int(args[0]))
    if sess is None:
        await query.answer("Oturum yok.", show_alert=True)
        return
    if sess.ended_at is None:
        text, kb = await screen_active_session(sess)
    else:
        text, kb = await screen_session_notes(sess.id)
    await _send_screen(update, context, text, kb)


async def _cb_session_notes(update, context, args):
    text, kb = await screen_session_notes(int(args[0]))
    await _send_screen(update, context, text, kb)


async def _cb_open_sessions(update, context, args):
    text, kb = await screen_open_sessions()
    await _send_screen(update, context, text, kb)


async def _cb_start_pick(update, context, args):
    query = update.callback_query
    books = await data.list_books()
    unfinished = [b for b in books if b.status != BookStatus.FINISHED]
    if not unfinished:
        await query.answer("Devam edebileceğin kitap yok. Önce yeni bir kitap ekle.", show_alert=True)
        return
    kb = [[BTN(f"📖 {b.short_code} · {b.title}", _cb("recap", b.id))] for b in unfinished]
    kb.append([BTN("➕ Yeni Kitap Ekle", "newbook:start")])
    kb.append(_nav_row())
    await _send_screen(
        update, context,
        "🏠 Ana › ▶️ <b>Oturum Başlat</b>\n\nHangi kitabı okuyacaksın?", KB(kb),
    )


async def _cb_recap(update, context, args):
    text, kb = await screen_recap(int(args[0]))
    await _send_screen(update, context, text, kb)


async def _cb_begin(update, context, args):
    cid = _chat_id(update)
    book_id, page = int(args[0]), int(args[1])
    session = await data.start_session(book_id, page)
    if cid is not None:
        await _set(cid, "default_session", session.id, ttl_s=43200)
    text, kb = await screen_active_session(session)
    await _send_screen(update, context, text, kb)


async def _cb_begin_manual(update, context, args):
    await _prompt(update, context, "Başlangıç sayfası numarasını yaz ve gönder.",
                  {"type": "page_to_begin", "book_id": int(args[0])})


async def _cb_pause(update, context, args):
    query = update.callback_query
    cid = _chat_id(update)
    await data.pause_session(int(args[0]))
    await query.answer("⏸️ Duraklatıldı")
    if cid is not None:
        await _clear(cid, "default_session")
    text, kb = await screen_main()
    await _send_screen(update, context, text, kb)


async def _cb_end(update, context, args):
    cid = _chat_id(update)
    session_id = int(args[0])
    sess = await data.get_session(session_id)
    if cid is not None and sess:
        await _set(cid, "awaiting", {
            "type": "end_page", "session_id": session_id, "book_id": sess.book_id,
        })
    await _send_screen(
        update, context,
        "🏠 Ana › ⏹️ <b>Oturumu Bitir</b>\n\nŞu an hangi sayfadasın? Numarayı yaz.",
        KB([_nav_row()]),
    )


async def _cb_end_done(update, context, args):
    query = update.callback_query
    cid = _chat_id(update)
    session_id = int(args[0])
    awaiting = (await _get(cid, "awaiting")) if cid else None
    end_page = (awaiting or {}).get("end_page", 0)
    try:
        await data.end_session(session_id, end_page)
    except Exception as e:
        logger.error("bot.end_done.failed", error=str(e), exc_info=True)
    if cid is not None:
        await _clear(cid, "awaiting", "default_session")
    await query.answer("✅ Oturum kapatıldı")
    text, kb = await screen_main()
    await _send_screen(update, context, text, kb)


async def _cb_stats(update, context, args):
    text, kb = await screen_stats()
    await _send_screen(update, context, text, kb)


async def _cb_search(update, context, args):
    if not args or args[0] == "start":
        await _prompt(update, context,
                      "🔍 <b>Ara</b>\n\nAramak istediğin kelimeyi yaz ve gönder.",
                      {"type": "search"})


async def _cb_glossary(update, context, args):
    text, kb = await screen_glossary()
    await _send_screen(update, context, text, kb)


async def _cb_quotes(update, context, args):
    favorites_only = bool(args) and args[0] == "fav"
    text, kb = await screen_quotes(favorites_only=favorites_only)
    await _send_screen(update, context, text, kb)


async def _cb_route_pending(update, context, args):
    query = update.callback_query
    cid = _chat_id(update)
    sess = await data.get_session(int(args[0]))
    pending = (await _get(cid, "pending_input")) if cid else None
    if not sess or not isinstance(pending, dict):
        await query.answer("Pending bulunamadı.", show_alert=True)
        return
    if pending.get("kind") == "text" and pending.get("text"):
        if cid is not None:
            await _clear(cid, "pending_input")
            await _set(cid, "default_session", sess.id, ttl_s=43200)
        await _process_text_into_note(update, context, sess, pending["text"])
    else:
        if cid is not None:
            await _set(cid, "default_session", sess.id, ttl_s=43200)
            await _clear(cid, "pending_input")
        await query.message.reply_text(
            f"OK — sonraki notların <code>{esc(sess.code)}</code> oturumuna gidecek. "
            "Lütfen ses/foto'yu tekrar gönder.",
            parse_mode=ParseMode.HTML,
        )


async def _cb_route_pending_cancel(update, context, args):
    query = update.callback_query
    cid = _chat_id(update)
    if cid is not None:
        await _clear(cid, "pending_input")
    await query.answer("Vazgeçildi")


# ── voice:* (note-draft callbacks) ──

async def _cb_voice(update, context, args):
    query = update.callback_query
    cid = _chat_id(update)
    sub = args[0] if args else ""
    draft = await _get(cid, "draft_note") if cid else None
    if not isinstance(draft, dict):
        await query.answer("Aktif taslak not yok.", show_alert=True)
        return

    if sub == "cat":
        cat_value = args[1] if len(args) > 1 else draft["category"]
        try:
            cat_enum = Category(cat_value)
        except ValueError:
            cat_enum = Category.NEW_INFO
        draft["category"] = cat_enum.value
        draft["definition"] = (
            await ai.define_term(draft["transcript"], cat_enum)
            if cat_enum in (Category.WORD, Category.CONCEPT) else None
        )
        if cid is not None:
            await _set(cid, "draft_note", draft)
        text, kb = screen_note_confirm(draft)
        await _send_screen(update, context, text, kb)

    elif sub == "page":
        await _prompt(update, context, "Sayfa numarasını yaz ve gönder.",
                      {"type": "draft_page"})

    elif sub == "edit":
        await _prompt(update, context, "✏️ Düzeltilmiş transkripti yaz ve gönder.",
                      {"type": "draft_transcript"})

    elif sub == "skippage":
        draft["page"] = None
        if cid is not None:
            await _set(cid, "draft_note", draft)
            await _clear(cid, "awaiting")
        text, kb = screen_note_confirm(draft)
        await _send_screen(update, context, text, kb)

    elif sub == "explain":
        book = await data.get_book(draft["book_id"])
        try:
            draft["explanation"] = await ai.explain_note(draft["transcript"], book)
        except Exception as e:
            await query.answer(f"Açıklama üretilemedi: {type(e).__name__}", show_alert=True)
            return
        if cid is not None:
            await _set(cid, "draft_note", draft)
        text, kb = screen_note_confirm(draft)
        await _send_screen(update, context, text, kb)

    elif sub == "save":
        try:
            cat_enum = Category(draft["category"])
        except ValueError:
            cat_enum = Category.NEW_INFO
        try:
            await data.add_note(
                book_id=draft["book_id"], session_id=draft.get("session_id"),
                category=cat_enum, page=draft.get("page"),
                transcript=draft["transcript"],
                definition=draft.get("definition"), explanation=draft.get("explanation"),
            )
        except Exception as e:
            logger.error("bot.voice.save_failed", error=str(e), exc_info=True)
            await query.answer(f"Kayıt hatası: {type(e).__name__}", show_alert=True)
            return
        if cid is not None:
            await _clear(cid, "draft_note")
        await query.answer("✅ Not kaydedildi")
        sess_id = draft.get("session_id")
        session = await data.get_session(sess_id) if sess_id else None
        if session and session.ended_at is None:
            text, kb = await screen_active_session(session)
        else:
            text, kb = await screen_main()
        await _send_screen(update, context, text, kb)

    elif sub == "cancel":
        if cid is not None:
            await _clear(cid, "draft_note")
        await query.answer("İptal edildi")
        sess_id = draft.get("session_id")
        session = await data.get_session(sess_id) if sess_id else None
        if session and session.ended_at is None:
            text, kb = await screen_active_session(session)
            await _send_screen(update, context, text, kb)
            return
        text, kb = await screen_main()
        await _send_screen(update, context, text, kb)

    else:
        await query.answer(f"Bilinmeyen voice eylemi: {sub}", show_alert=True)


# ── question:* ──

async def _cb_question(update, context, args):
    query = update.callback_query
    cid = _chat_id(update)
    sub = args[0] if args else ""

    if sub == "ask":
        if len(args) < 2:
            await query.answer("Kitap belirsiz.", show_alert=True)
            return
        book_id = int(args[1])
        book = await data.get_book(book_id)
        if not book:
            await query.answer("Kitap yok.", show_alert=True)
            return
        if cid is not None:
            await _set(cid, "mode", {"name": "question", "book_id": book_id})
        text, kb = screen_question_idle(book)
        await _send_screen(update, context, text, kb)

    elif sub == "save":
        book_id = int(args[1]) if len(args) > 1 else None
        qa = await _get(cid, "last_qa") if cid else None
        if not isinstance(qa, dict) or book_id is None:
            await query.answer("Kaydedilecek soru-cevap yok.", show_alert=True)
            return
        sess = await data.active_session_for_book(book_id)
        try:
            await data.add_note(
                book_id=book_id, session_id=sess.id if sess else None,
                category=Category.CONCEPT, page=sess.start_page if sess else None,
                transcript=f"Soru: {qa['question']}\n\nCevap: {qa['answer']}",
                from_qa=True,
            )
        except Exception as e:
            logger.error("bot.question.save_failed", error=str(e), exc_info=True)
            await query.answer(f"Kayıt hatası: {type(e).__name__}", show_alert=True)
            return
        if cid is not None:
            await _clear(cid, "last_qa")
        await query.answer("✅ Q&A not olarak kaydedildi")
        text, kb, cover = await screen_book_detail(book_id)
        await _send_screen(update, context, text, kb, photo_url=cover)


# ── export:* ──

async def _cb_export(update, context, args):
    query = update.callback_query
    chat_id = query.message.chat_id

    if not args or args[0] == "menu":
        text, kb = screen_export_menu()
        await _send_screen(update, context, text, kb)
        return

    head = args[0]

    if head == "choose":
        fmt = args[1] if len(args) > 1 else "pdf"
        books = await data.list_books()
        kb = [[BTN(f"📖 {b.short_code} · {b.title}", _cb("export", fmt, b.id))] for b in books]
        kb.append(_nav_row())
        await _send_screen(
            update, context, f"Hangi kitabı dışa aktarayım? ({fmt.upper()})", KB(kb),
        )
        return

    if head == "all" and len(args) >= 2 and args[1] == "json":
        await query.answer("JSON arşivi üretiliyor…")
        try:
            books = await data.list_books()
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for b in books:
                    zf.writestr(f"{_safe_filename(b.title)}.json",
                                await data.export_json(b.id))
            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(io.BytesIO(buf.getvalue()), filename="kitabi-data.zip"),
                caption="📦 Tüm veri JSON arşivi",
            )
        except Exception as e:
            logger.error("bot.export.all_json.failed", error=str(e), exc_info=True)
            await query.message.reply_text(
                f"❌ Hata: <code>{esc(type(e).__name__)}</code>", parse_mode=ParseMode.HTML,
            )
        return

    if head == "all" and len(args) >= 2 and args[1] == "zip":
        await query.answer("ZIP arşivi üretiliyor…")
        try:
            zip_bytes = await data.export_all_zip()
            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(io.BytesIO(zip_bytes), filename="kitabi-all.zip"),
                caption="🗂️ Tüm kitaplar (PDF + JSON + Markdown)",
            )
        except Exception as e:
            logger.error("bot.export.all_zip.failed", error=str(e), exc_info=True)
            await query.message.reply_text(
                f"❌ Hata: <code>{esc(type(e).__name__)}</code>", parse_mode=ParseMode.HTML,
            )
        return

    if head in ("pdf", "csv", "md") and len(args) >= 2:
        book_id = int(args[1])
        book = await data.get_book(book_id)
        if not book:
            await query.message.reply_text("Kitap bulunamadı.")
            return
        safe = _safe_filename(book.title)
        try:
            if head == "pdf":
                bts = await data.render_pdf(book_id)
                filename, caption = f"{safe}.pdf", f"📕 {book.title}"
            elif head == "csv":
                bts = await data.export_csv(book_id)
                filename, caption = f"{safe}.csv", f"📊 {book.title} — notlar (CSV)"
            else:
                bts = await data.export_markdown(book_id)
                filename, caption = f"{safe}.md", f"📝 {book.title} — Markdown"
            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(io.BytesIO(bts), filename=filename),
                caption=caption,
            )
        except Exception as e:
            logger.error("bot.export.single.failed",
                         format=head, book_id=book_id, error=str(e), exc_info=True)
            await query.message.reply_text(
                f"❌ {head.upper()} üretilirken hata: <code>{esc(type(e).__name__)}</code>",
                parse_mode=ParseMode.HTML,
            )


# ── newbook:* ──

async def _cb_newbook(update, context, args):
    query = update.callback_query
    cid = _chat_id(update)
    sub = args[0] if args else "start"

    if sub == "start":
        text, kb = screen_newbook_method()
        await _send_screen(update, context, text, kb)

    elif sub == "isbn":
        await _prompt(update, context,
                      "🏠 Ana › ➕ Yeni Kitap › 🔢 <b>ISBN</b>\n\n"
                      "ISBN numarasını yaz ve gönder.\n"
                      "13 haneli (978...) veya 10 haneli olabilir.",
                      {"type": "newbook_isbn"})

    elif sub == "manual":
        await _prompt(update, context,
                      "🏠 Ana › ➕ Yeni Kitap › ✍️ <b>Elle</b>\n\nKitabın adını yaz ve gönder.",
                      {"type": "newbook_title"})

    elif sub == "save":
        draft = await _get(cid, "newbook_draft") if cid else None
        if not isinstance(draft, dict):
            await query.answer("Önizleme verisi kayıp.", show_alert=True)
            return
        book = await data.create_book(
            title=draft.get("title") or "(başlıksız)",
            author=draft.get("author"), isbn=draft.get("isbn"),
            genre=draft.get("genre"), subgenre=draft.get("subgenre"),
            total_pages=draft.get("total_pages"),
            cover_url=draft.get("cover_url"),
            goodreads_url=draft.get("goodreads_url"),
        )
        if cid is not None:
            await _clear(cid, "newbook_draft")
        await query.answer("✅ Eklendi")
        await _show_onboarding_or_book(update, context, book)

    elif sub == "codefirst":
        draft = await _get(cid, "newbook_draft") if cid else None
        if not isinstance(draft, dict):
            await query.answer("Önizleme verisi kayıp.", show_alert=True)
            return
        pending = {k: draft.get(k) for k in (
            "title", "author", "isbn", "genre", "subgenre",
            "total_pages", "cover_url", "goodreads_url",
        )}
        pending["title"] = pending["title"] or "(başlıksız)"
        if cid is not None:
            await _set(cid, "awaiting", {"type": "newbook_short_code", "pending_book": pending})
            await _clear(cid, "newbook_draft")
        await _send_screen(
            update, context,
            "🔤 <b>Kısa kod</b>\n\n2-8 büyük harf/rakam. Örn: <code>SVC</code>, "
            "<code>1984</code>, <code>ELIK</code>.",
            KB([_nav_row()]),
        )


# ── book_edit:* ──

async def _cb_book_edit(update, context, args):
    book_id = int(args[0])
    sub_args = args[1:]
    if not sub_args:
        book = await data.get_book(book_id)
        if not book:
            await update.callback_query.answer("Kitap yok.", show_alert=True)
            return
        text = f"✏️ <b>{esc(book.title)}</b> — neyi değiştireyim?"
        kb = [
            [BTN("🏷️ Etiketler",  _cb("book_edit", book.id, "tags"))],
            [BTN("📝 Kişisel not", _cb("book_edit", book.id, "personal_note"))],
            [BTN("📄 Toplam sayfa", _cb("book_edit", book.id, "total_pages"))],
            [BTN(f"🔤 Kısa kod ({book.short_code})", _cb("book_edit", book.id, "short_code"))],
            _nav_row(),
        ]
        await _send_screen(update, context, text, KB(kb))
        return
    field = sub_args[0]
    prompts = {
        "tags": "Etiketleri virgülle ayır ve gönder (örn: <code>felsefe, klasik, varoluş</code>).",
        "personal_note": "Kitap hakkındaki kişisel notunu yaz.",
        "total_pages": "Toplam sayfa sayısını yaz (sadece sayı).",
        "short_code": "Yeni kısa kod (2-8 büyük harf/rakam).",
    }
    await _prompt(update, context,
                  prompts.get(field, "Yeni değeri gönder."),
                  {"type": "book_edit_field", "book_id": book_id, "field": field})


# ── book_del:* ──

async def _cb_book_del(update, context, args):
    query = update.callback_query
    if not args:
        return
    sub = args[0]
    book_id = int(args[1]) if len(args) > 1 else None
    if not book_id:
        return
    book = await data.get_book(book_id)
    if not book:
        await query.answer("Kitap yok.", show_alert=True)
        return
    if sub == "ask":
        text = (
            f"🗑️ <b>{esc(book.title)}</b> ({book.short_code}) silinecek.\n\n"
            f"{len(book.sessions)} oturum, {len(book.notes)} not da silinir. Emin misin?"
        )
        kb = [[BTN("✅ Evet, sil", _cb("book_del", "yes", book_id)),
               BTN("❌ Vazgeç",   _cb("book", book_id))]]
        await _send_screen(update, context, text, KB(kb))
    elif sub == "yes":
        await data.delete_book(book_id)
        await query.answer("🗑️ Silindi")
        text, kb = await screen_book_list()
        await _send_screen(update, context, text, kb)


# ── finish:* ──

async def _cb_finish(update, context, args):
    if not args:
        return
    sub = args[0]
    book_id = int(args[1]) if len(args) > 1 else None
    book = await data.get_book(book_id) if book_id else None
    if not book:
        return

    if sub == "start":
        await data.update_book(book_id, status=BookStatus.FINISHED)
        sess = await data.active_session_for_book(book_id)
        if sess:
            await data.end_session(sess.id, sess.start_page or 0)
        book = await data.get_book(book_id)
        text, kb = screen_finish_rating(book)
        await _send_screen(update, context, text, kb)

    elif sub == "rate":
        await data.update_book(book_id, rating=int(args[2]))
        await _begin_finish_review(update, context, book_id)

    elif sub == "skip_rate":
        await _begin_finish_review(update, context, book_id)

    elif sub == "skip_review":
        text, kb = await screen_finish_favorites(book)
        await _send_screen(update, context, text, kb)

    elif sub == "toggle_fav":
        note_id = int(args[2])
        note = await data.get_note(note_id)
        if note:
            await data.update_note(note_id, is_favorite=not note.is_favorite)
        text, kb = await screen_finish_favorites(book)
        await _send_screen(update, context, text, kb)

    elif sub == "to_recommend":
        text, kb = screen_finish_recommend(book)
        await _send_screen(update, context, text, kb)

    elif sub == "rec":
        choice = args[2]
        if choice == "yes":
            await data.update_book(book_id, would_recommend=True)
        elif choice == "no":
            await data.update_book(book_id, would_recommend=False)
        await _emit_finish_pdf(update, context, book_id)


async def _begin_finish_review(update, context, book_id):
    cid = _chat_id(update)
    if cid is not None:
        await _set(cid, "awaiting", {"type": "finish_review", "book_id": book_id})
    book = await data.get_book(book_id)
    text, kb = screen_finish_review_prompt(book)
    await _send_screen(update, context, text, kb)


async def _emit_finish_pdf(update, context, book_id):
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer("PDF üretiliyor…")
    try:
        pdf_bytes = await data.render_pdf(book_id)
        book = await data.get_book(book_id)
        rating_str = ("⭐" * book.rating) if book.rating else "-"
        parts = [
            f"📕 <b>{esc(book.title)}</b> — okuma günlüğün hazır.", "",
            f"Puan: {rating_str}",
        ]
        if book.one_line_review:
            parts.append(f"Yorum: <i>{esc(book.one_line_review)}</i>")
        await context.bot.send_document(
            chat_id=chat_id,
            document=InputFile(io.BytesIO(pdf_bytes), filename=f"{_safe_filename(book.title)}.pdf"),
            caption="\n".join(parts), parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("bot.finish.pdf_failed", book_id=book_id, error=str(e), exc_info=True)
        await query.message.reply_text(
            f"❌ PDF üretilirken hata: <code>{esc(type(e).__name__)}</code>",
            parse_mode=ParseMode.HTML,
        )
    text, kb, cover = await screen_book_detail(book_id)
    await _send_screen(update, context, text, kb, photo_url=cover)


# ── settings:* ──

async def _cb_settings(update, context, args):
    if args:
        sub = args[0]
        if sub == "toggle":
            field = args[1]
            s = await data.get_settings()
            await data.update_settings(**{field: not getattr(s, field)})
        elif sub == "nudge_interval":
            delta = int(args[1])
            s = await data.get_settings()
            new_val = max(1, min(30, s.nudge_interval_days + delta))
            await data.update_settings(nudge_interval_days=new_val)
    text, kb = await screen_settings()
    await _send_screen(update, context, text, kb)


# ── final dispatch dict ──
_CALLBACKS: dict[str, Callable[..., Awaitable[None]]] = {
    "noop":                  _cb_noop,
    "main":                  _cb_main,
    "back":                  _cb_main,
    "books":                 _cb_books,
    "book":                  _cb_book,
    "notes":                 _cb_notes,
    "note":                  _cb_note,
    "note_fav":              _cb_note_fav,
    "note_edit":             _cb_note_edit,
    "note_page":             _cb_note_page,
    "note_del":              _cb_note_del,
    "sessions":              _cb_sessions,
    "session_open":          _cb_session_open,
    "session_notes":         _cb_session_notes,
    "open_sessions":         _cb_open_sessions,
    "start_pick":            _cb_start_pick,
    "recap":                 _cb_recap,
    "begin":                 _cb_begin,
    "begin_manual":          _cb_begin_manual,
    "voice":                 _cb_voice,
    "question":              _cb_question,
    "pause":                 _cb_pause,
    "end":                   _cb_end,
    "end_done":              _cb_end_done,
    "finish":                _cb_finish,
    "export":                _cb_export,
    "newbook":               _cb_newbook,
    "book_edit":             _cb_book_edit,
    "book_del":              _cb_book_del,
    "settings":              _cb_settings,
    "stats":                 _cb_stats,
    "search":                _cb_search,
    "glossary":              _cb_glossary,
    "quotes":                _cb_quotes,
    "route_pending":         _cb_route_pending,
    "route_pending_cancel":  _cb_route_pending_cancel,
}


# ────────────────────────── proactive nudges ──────────────────────────


async def send_proactive_nudges(
    application: Application, allowed_user_ids: list[int]
) -> int:
    """Proactive nudge job — called by Cloud Scheduler every ~2 hours.

    Three-tier logic for open sessions:
      • >12h open  → auto-pause (session was forgotten)
      • 2–6h open  → send "🕐 Hâlâ okuyor musun?" reminder (once per session)
      • idle book  → poke about books not touched in `nudge_interval_days`
    """
    s = await data.get_settings()
    if not s.nudge_enabled:
        logger.info("bot.nudge.disabled")
        return 0

    now = datetime.now(timezone.utc)
    auto_pause_cutoff = now - timedelta(hours=12)
    still_reading_lo  = now - timedelta(hours=6)
    still_reading_hi  = now - timedelta(hours=2)

    open_sessions = await data.list_open_sessions()
    paused = 0
    still_reading_sent = 0
    for sess in open_sessions:
        started = sess.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        # Tier 1: stale → pause
        if started < auto_pause_cutoff:
            await data.pause_session(sess.id)
            paused += 1
            continue
        # Tier 2: 2–6h → "still reading?" once per session
        if still_reading_lo <= started <= still_reading_hi:
            key = f"still_reading_sent_s{sess.id}"
            for uid in allowed_user_ids:
                if await data.get_ephemeral(uid, key):
                    continue
                book = await data.get_book(sess.book_id)
                title = book.title if book else "?"
                try:
                    await application.bot.send_message(
                        chat_id=uid,
                        text=(
                            f"🕐 <b>Hâlâ okuyor musun?</b>\n\n"
                            f"📖 <b>{esc(title)}</b> oturumun "
                            f"<code>{esc(sess.code)}</code> hâlâ açık. "
                            f"Bitirmek istersen aşağıdan kapatabilirsin."
                        ),
                        reply_markup=KB([
                            [BTN("⏹️ Bitir", _cb("end", sess.id))],
                            [BTN("✅ Hâlâ okuyorum", "noop")],
                        ]),
                        parse_mode=ParseMode.HTML,
                    )
                    await data.set_ephemeral(uid, key, True, ttl_s=24*3600)
                    still_reading_sent += 1
                except Exception as e:
                    logger.warning("bot.nudge.still_reading_failed", user_id=uid, error=str(e))
    if paused:
        logger.info("bot.nudge.auto_paused", count=paused)
    if still_reading_sent:
        logger.info("bot.nudge.still_reading_sent", count=still_reading_sent)

    idle = await data.books_idle_since(s.nudge_interval_days)
    sent = 0
    for uid in allowed_user_ids:
        for book in idle[:5]:
            summaries = await data.summaries_for_book(book.id)
            last_summary = summaries[-1] if summaries else None
            lines = [
                f"📖 <b>{esc(book.title)}</b> uzun süredir bekliyor.",
                f"Son not alındığı sayfa: <b>s.{book.read_pages or '—'}</b>",
            ]
            if last_summary:
                lines += ["", "<i>Son özetin:</i>",
                          f"<i>“{esc(last_summary.transcript[:200])}”</i>"]
            kb = KB([
                [BTN("▶️ Devam et", _cb("recap", book.id))],
                [BTN("🔕 Hatırlatmaları kapat", "settings:toggle:nudge_enabled")],
            ])
            try:
                await application.bot.send_message(
                    chat_id=uid, text="\n".join(lines), reply_markup=kb,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
                sent += 1
            except Exception as e:
                logger.warning("bot.nudge.send_failed", user_id=uid, error=str(e))
    return sent + still_reading_sent


# ────────────────────────── application setup ──────────────────────────


def build_application(
    token: str,
    *,
    allowed_user_ids: list[int] | None = None,
    mode: str = "webhook",
) -> Application:
    """Build the python-telegram-bot Application and register handlers.

    `mode="webhook"`: no Updater; allowlist enforced in main.py before dispatch.
    `mode="polling"`: default Updater + per-handler `filters.User(...)`.
    """
    logger.info("bot.build_application.start",
                mode=mode, allowlist_size=len(allowed_user_ids or []))
    builder = Application.builder().token(token)
    if mode == "webhook":
        builder = builder.updater(None)
    application = builder.build()

    user_filter = filters.User(user_id=allowed_user_ids) if allowed_user_ids else None
    if user_filter is not None:
        application.add_handler(CommandHandler("start", handle_start_command, filters=user_filter))
        application.add_handler(MessageHandler(filters.VOICE & user_filter, handle_voice))
        application.add_handler(MessageHandler(filters.PHOTO & user_filter, handle_photo))
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & user_filter, handle_text))
    else:
        application.add_handler(CommandHandler("start", handle_start_command))
        application.add_handler(MessageHandler(filters.VOICE, handle_voice))
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("bot.build_application.success")
    return application


async def set_bot_commands(application: Application) -> None:
    logger.info("bot.set_bot_commands.start")
    await application.bot.set_my_commands([BotCommand("start", "Ana menüye dön")])
    logger.info("bot.set_bot_commands.success")
