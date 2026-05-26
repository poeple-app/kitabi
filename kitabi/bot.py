"""Telegram bot logic for Kitabi (v1.0.5)."""

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
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
    KeyboardButton, ReplyKeyboardMarkup, Update,
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


# Target row count: anchor for UI consistency across screens.
# Screens with fewer rows get padded with invisible rows so the keyboard
# height stays stable. Cover-photo screens pass `pad=False` because they
# already render large (image + caption) and benefit from a compact KB.
_MIN_KB_ROWS = 5


def _is_nav_row(row: list[InlineKeyboardButton]) -> bool:
    """Detect a `_nav_row()`-style row so padding can be inserted above it."""
    return any(b.callback_data in ("main", "back") for b in row)


def _pad_rows(
    rows: list[list[InlineKeyboardButton]], min_rows: int = _MIN_KB_ROWS,
) -> list[list[InlineKeyboardButton]]:
    """Insert invisible-button rows so the keyboard always has at least
    `min_rows` rows. If the last row is a nav row (Back/Home), padding is
    inserted above it so the nav stays at the bottom; otherwise padding is
    appended after the last row.
    """
    if len(rows) >= min_rows:
        return rows
    pad_row = [InlineKeyboardButton(" ", callback_data="noop")]
    need = min_rows - len(rows)
    if not rows:
        return [pad_row] * min_rows
    if _is_nav_row(rows[-1]):
        return rows[:-1] + [pad_row] * need + rows[-1:]
    return rows + [pad_row] * need


def KB(
    rows: list[list[InlineKeyboardButton]], *, pad: bool = True,
) -> InlineKeyboardMarkup:
    """Build an InlineKeyboardMarkup. Pads to `_MIN_KB_ROWS` by default so
    keyboard heights stay consistent across screens. Pass `pad=False` on
    cover-photo screens (they're already large)."""
    return InlineKeyboardMarkup(_pad_rows(rows) if pad else rows)


def _nav_row(include_back: bool = True) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if include_back:
        row.append(BTN("⬅️ Geri", "back"))
    row.append(BTN("🏠 Ana Menü", "main"))
    return row


def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "kitabi"


# v1.0.2 — Persistent reply keyboard (Madde 5).
# These buttons sit below the chat input box; tapping one sends the button's
# label as a plain text message. `handle_text` matches the label and routes
# to the right screen.
_QUICK_OTURUMLAR = "🟢 Oturumlar"
_QUICK_BITIR     = "⏹️ Bitir"
_QUICK_KITAPLAR  = "📖 Kitaplar"
_QUICK_YENI      = "➕ Yeni"


def _quick_keyboard() -> ReplyKeyboardMarkup:
    """Build the persistent quick-action reply keyboard (Madde 5)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(_QUICK_OTURUMLAR), KeyboardButton(_QUICK_BITIR)],
            [KeyboardButton(_QUICK_KITAPLAR),  KeyboardButton(_QUICK_YENI)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Sesli not, fotoğraf veya yazı gönder…",
    )


_QUICK_LABELS = {_QUICK_OTURUMLAR, _QUICK_BITIR, _QUICK_KITAPLAR, _QUICK_YENI}


async def _track_last_menu(cid: int | None, message_id: int) -> None:
    """Remember the most recent bot 'menu' message so future renders can delete
    it — keeping exactly one active inline menu in the chat at a time (Madde 3).
    """
    if cid is None:
        return
    try:
        await _set(cid, "last_menu_msg_id", message_id, ttl_s=24 * 3600)
    except Exception as e:
        logger.debug("bot.track_last_menu.failed", error=str(e))


async def _delete_previous_menu(
    cid: int | None, context: ContextTypes.DEFAULT_TYPE,
    *, except_id: int | None = None,
) -> None:
    """If we have a tracked previous menu, try to delete it. Best-effort —
    Telegram refuses to delete messages older than 48h or with no permissions;
    we swallow those errors.
    """
    if cid is None:
        return
    prev = await _get(cid, "last_menu_msg_id")
    if not isinstance(prev, int) or prev == except_id:
        return
    try:
        await context.bot.delete_message(chat_id=cid, message_id=prev)
    except Exception as e:
        logger.debug("bot.delete_previous_menu.failed", error=str(e))


async def _send_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup,
    *,
    photo_url: str | None = None,
    photo_file_id: str | None = None,
) -> None:
    """Edit-in-place when triggered by callback; new bubble when user sent input.

    v1.0.2: also deletes the *previously* tracked menu bubble so the user only
    ever sees ONE active inline keyboard in the chat (Madde 3). The current
    message becomes the new "active menu" via _track_last_menu.

    Handles photo↔text transitions by delete+send (Telegram can't edit between).
    """
    cid = _chat_id(update)
    sent_msg = None
    # Telegram captions are limited to ~1024 chars; trim long screens when
    # we have to attach the text to a photo.
    photo_obj = photo_url or photo_file_id
    if photo_obj and len(text) > 1000:
        # Move long captions to a follow-up text message
        long_caption_overflow = text
        text = text[:1000].rsplit("\n", 1)[0] + "\n\n<i>(devamı aşağıda)</i>"
    else:
        long_caption_overflow = None

    if update.callback_query:
        msg = update.callback_query.message
        # If a *different* tracked menu lingers in the chat (e.g. user navigated
        # via reply keyboard, leaving an older inline menu floating), delete it
        # before rendering the new one. The current message is what we're about
        # to edit/replace, so it's excluded.
        await _delete_previous_menu(cid, context, except_id=msg.message_id)
        try:
            if photo_obj:
                # Photo screens always delete + send (Telegram can't edit text↔photo)
                await msg.delete()
                sent_msg = await context.bot.send_photo(
                    chat_id=msg.chat_id, photo=photo_obj,
                    caption=text, reply_markup=keyboard, parse_mode=ParseMode.HTML,
                )
            elif msg.photo:
                await msg.delete()
                sent_msg = await context.bot.send_message(
                    chat_id=msg.chat_id, text=text, reply_markup=keyboard,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
            else:
                # Edit-in-place: same message stays the "active menu"
                sent_msg = await msg.edit_text(
                    text, reply_markup=keyboard,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
        except Exception as e:
            logger.warning("bot.send_screen.fallback", error=str(e))
            sent_msg = await context.bot.send_message(
                chat_id=msg.chat_id, text=text, reply_markup=keyboard,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
    elif update.message:
        # Fresh bubble in response to user input. Delete the previously tracked
        # menu first so the chat stays clean.
        await _delete_previous_menu(cid, context)
        if photo_obj:
            sent_msg = await update.message.reply_photo(
                photo=photo_obj, caption=text,
                reply_markup=keyboard, parse_mode=ParseMode.HTML,
            )
        else:
            sent_msg = await update.message.reply_text(
                text, reply_markup=keyboard,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )

    # If the photo caption was trimmed, send the rest as a follow-up.
    if long_caption_overflow and sent_msg is not None:
        try:
            await context.bot.send_message(
                chat_id=sent_msg.chat_id,
                text=long_caption_overflow[1000:],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.debug("bot.send_screen.overflow_failed", error=str(e))

    # Track the new active menu
    if sent_msg is not None and hasattr(sent_msg, "message_id"):
        await _track_last_menu(cid, sent_msg.message_id)


async def _send_menu_reply(
    msg, context: ContextTypes.DEFAULT_TYPE,
    text: str, keyboard: InlineKeyboardMarkup,
    *, photo_file_id: str | None = None,
) -> None:
    """Reply to a user message with a NEW menu bubble, deleting the previously
    tracked menu first. Use this anywhere we previously did
    `msg.reply_text(..., reply_markup=kb)` outside of `_send_screen`
    (screen_note_confirm follow-ups, end_session reply, etc.). This is the
    "Madde 9 — tek aktif menü" guarantee for the input-driven path.
    """
    cid = msg.chat_id if msg else None
    await _delete_previous_menu(cid, context)
    if photo_file_id:
        sent = await msg.reply_photo(
            photo=photo_file_id, caption=text[:1000],
            reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )
    else:
        sent = await msg.reply_text(
            text, reply_markup=keyboard,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    if sent is not None and hasattr(sent, "message_id"):
        await _track_last_menu(cid, sent.message_id)


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


class _Progress:
    """Async context manager: shows a transient "🔄 …" message to the user
    while a long-running op runs, then deletes it.

    Usage:
        async with _Progress(msg, "🎤 Ses metne dönüştürülüyor…"):
            transcript = await ai.transcribe_voice(...)
    """
    def __init__(self, msg, text: str):
        self._msg = msg
        self._text = text
        self._placeholder = None

    async def __aenter__(self):
        try:
            await self._msg.chat.send_action(ChatAction.TYPING)
            self._placeholder = await self._msg.reply_text(
                self._text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
        except Exception as e:
            logger.debug("bot.progress.start_failed", error=str(e))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._placeholder is not None:
            try:
                await self._placeholder.delete()
            except Exception as e:
                logger.debug("bot.progress.delete_failed", error=str(e))


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
    """Main menu — adapts to library state, shows live stats.

    Empty library → onboarding CTA.
    Otherwise → counts, greeting, last-read book, this month's reading.
    """
    books = await data.list_books()
    open_sessions = await data.list_open_sessions()
    if not books:
        text = (
            "🏠 <b>Ana Menü</b>\n\n"
            "Selam! Henüz hiç kitabın yok.\n"
            "Her şey kitap eklemekle başlıyor — yazarak, ISBN ile ya da kitap kapağının fotoğrafını çekerek ekleyebilirsin.\n\n"
            "Ekledikten sonra sana neler yapabileceğini tanıtırım."
        )
        return text, KB([[BTN("➕ İlk Kitabını Ekle", "newbook:start")]])

    # Live stats: total books, finished, reading, this-month sessions + minutes
    s_month = await data.compute_stats(period_days=30)
    finished = sum(1 for b in books if b.status == BookStatus.FINISHED)
    reading = sum(1 for b in books if b.status == BookStatus.READING)
    # Find most recently active book (last session)
    last_book_line = ""
    sorted_books = [b for b in books if b.status != BookStatus.FINISHED]
    if sorted_books:
        # Pick the book with the most recent session
        candidate = None
        latest_dt = None
        for b in sorted_books:
            sessions = await data.list_sessions_for_book(b.id)
            if sessions:
                start = sessions[0].started_at  # already sorted desc
                if latest_dt is None or (start and start > latest_dt):
                    latest_dt = start
                    candidate = b
        if candidate:
            last_book_line = (
                f"\n📖 Son okuduğun: <b>{esc(candidate.title)}</b>  "
                f"·  s.{candidate.read_pages or 0}/{candidate.total_pages or '?'}"
            )

    parts = [
        "🏠 <b>Ana Menü</b>",
        "",
        f"📚 Kütüphane: <b>{len(books)}</b> kitap  ·  ✅ {finished} bitti  ·  📖 {reading} okuyor",
        f"🗓️ Bu ay: <b>{s_month['session_count']}</b> oturum  ·  "
        f"⏱️ <b>{s_month['total_hours']} sa {s_month['total_min_rem']} dk</b>",
    ]
    if s_month.get("streak_days"):
        parts.append(f"🔥 Streak: <b>{s_month['streak_days']} gün</b>")
    if open_sessions:
        parts.append(f"\n🟢 <b>{len(open_sessions)} açık oturum</b> var — devam etmek ister misin?")
    if last_book_line:
        parts.append(last_book_line)

    kb: list[list[InlineKeyboardButton]] = []
    if open_sessions:
        kb.append([BTN(f"🟢 Açık Oturumlar ({len(open_sessions)})", "open_sessions")])
    kb += [
        [BTN("▶️ Oturum Başlat", "start_pick")],
        [BTN("📚 Kitaplarım", "books"), BTN("➕ Yeni Kitap", "newbook:start")],
        [BTN("📝 Notlarım", "notes_hub"), BTN("🔍 Ara", "search:start")],
        [BTN("📖 Sözlük", "glossary"), BTN("📊 İstatistik", "stats")],
        [BTN("📤 Dışa Aktar", "export:menu"), BTN("⚙️ Ayarlar", "settings")],
    ]
    return "\n".join(parts), KB(kb)


async def screen_notes_hub() -> ScreenResult:
    """Top-level notes browser: every category as its own button with a live
    note count. Includes user-defined custom categories and a "➕ Yeni
    kategori" entry point. Sözlük stays in screen_main (kullanıcı kararı).
    """
    counts = await data.count_notes_by_category()
    custom_counts = await data.count_notes_by_custom_label()
    settings = await data.get_settings()
    custom_cats = list(getattr(settings, "custom_categories", None) or [])
    all_quotes_n = counts.get("QUOTE", 0)
    fav_quotes_n = 0
    if all_quotes_n:
        favs = [q for q, _ in await data.list_quotes(favorites_only=True)]
        fav_quotes_n = len(favs)

    text = (
        "🏠 Ana › 📝 <b>Notlarım</b>\n\n"
        f"💬 Alıntı: <b>{all_quotes_n}</b>  ·  ⭐ {fav_quotes_n} favori\n"
        f"💡 Fikir: <b>{counts.get('IDEA', 0)}</b>\n"
        f"📚 Yeni Bilgi: <b>{counts.get('NEW_INFO', 0)}</b>\n"
        f"🧠 Kavram: <b>{counts.get('CONCEPT', 0)}</b>\n"
        f"🔤 Kelime: <b>{counts.get('WORD', 0)}</b>\n"
        f"📋 Özet: <b>{counts.get('SUMMARY', 0)}</b>"
    )
    if custom_cats:
        text += "\n\n<i>Kendi kategorilerin:</i>"
        for cc in custom_cats:
            n = custom_counts.get(cc, 0)
            text += f"\n  • {esc(cc)}: <b>{n}</b>"
    text += "\n\n<i>Kelime + Kavram için ayrıca ana menüde 📖 Sözlük var.</i>"

    kb: list[list[InlineKeyboardButton]] = [
        [BTN(f"💬 Alıntılar ({all_quotes_n})", "quotes:all"),
         BTN(f"⭐ Favori ({fav_quotes_n})",     "quotes:fav")],
        [BTN(f"💡 Fikir ({counts.get('IDEA', 0)})",         _cb("notes_cat", "IDEA")),
         BTN(f"📚 Yeni Bilgi ({counts.get('NEW_INFO', 0)})", _cb("notes_cat", "NEW_INFO"))],
        [BTN(f"🧠 Kavram ({counts.get('CONCEPT', 0)})",     _cb("notes_cat", "CONCEPT")),
         BTN(f"📋 Özet ({counts.get('SUMMARY', 0)})",       _cb("notes_cat", "SUMMARY"))],
        [BTN(f"🔤 Kelime ({counts.get('WORD', 0)})",        _cb("notes_cat", "WORD")),
         BTN("📖 Sözlük (Kel.+Kav.)", "glossary")],
    ]
    # User-defined custom categories
    if custom_cats:
        row: list[InlineKeyboardButton] = []
        for cc in custom_cats:
            n = custom_counts.get(cc, 0)
            row.append(BTN(f"🏷️ {cc} ({n})", _cb("notes_custom", cc)))
            if len(row) == 2:
                kb.append(row); row = []
        if row:
            kb.append(row)
    kb.append([BTN("➕ Yeni kategori ekle", "notes_cat_new")])
    kb.append([BTN("🔍 Notlarda ara", "search:start")])
    kb.append(_nav_row())
    return text, KB(kb)


async def screen_notes_by_custom_label(label: str, limit: int = 5) -> ScreenResult:
    """List notes whose custom category_label matches. Used by the user-defined
    category buttons in screen_notes_hub.
    """
    results = await data.notes_by_custom_label(label)
    total = len(results)
    lines = [
        f"🏠 Ana › 📝 Notlarım › <b>🏷️ {esc(label)}</b>  ({total})", "",
    ]
    kb: list[list[InlineKeyboardButton]] = []
    if total == 0:
        lines.append("<i>(Bu kategoride henüz not yok.)</i>")
    else:
        showing = min(limit, total)
        lines.append(f"{showing}/{total} gösteriliyor\n")
        for note, book in results[:limit]:
            snip = note.transcript[:80] + ("…" if len(note.transcript) > 80 else "")
            lines.append(
                f"<code>{esc(note.code)}</code> · {esc(book.title)}\n  <i>{esc(snip)}</i>"
            )
            kb.append([BTN(f"{note.code} · {book.short_code}", _cb("note", note.id))])
        if showing < total:
            kb.append([BTN(
                f"⬇️ {min(_LIST_PAGE_SIZE, total - showing)} not daha göster",
                _cb("notes_custom", label, "more", limit + _LIST_PAGE_SIZE),
            )])
    kb.append([BTN(f"🗑️ \"{label}\" kategorisini sil", _cb("notes_cat_del", label))])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


async def screen_notes_by_category(
    category_name: str, limit: int = 5,
) -> ScreenResult:
    """List notes filtered by category, with pagination (Madde 14 semantik)."""
    try:
        cat = Category[category_name]
    except KeyError:
        return "Bilinmeyen kategori.", KB([_nav_row()])
    results = await data.notes_by_category(cat)
    total = len(results)
    icon = {
        "QUOTE": "💬", "IDEA": "💡", "NEW_INFO": "📚",
        "WORD": "🔤", "CONCEPT": "🧠", "SUMMARY": "📋",
    }.get(category_name, "📝")
    lines = [f"🏠 Ana › 📝 Notlarım › <b>{icon} {esc(cat.value)}</b>  ({total})", ""]
    kb: list[list[InlineKeyboardButton]] = []
    if total == 0:
        lines.append("<i>(Bu kategoride henüz not yok.)</i>")
    else:
        showing = min(limit, total)
        lines.append(f"{showing}/{total} gösteriliyor\n")
        for note, book in results[:limit]:
            snip = note.transcript[:80] + ("…" if len(note.transcript) > 80 else "")
            lines.append(
                f"<code>{esc(note.code)}</code> · {esc(book.title)}\n  <i>{esc(snip)}</i>"
            )
            kb.append([BTN(f"{note.code} · {book.short_code}", _cb("note", note.id))])
        if showing < total:
            kb.append([BTN(
                f"⬇️ {min(_LIST_PAGE_SIZE, total - showing)} not daha göster",
                _cb("notes_cat", category_name, "more", limit + _LIST_PAGE_SIZE),
            )])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


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


async def screen_book_list(shelf_filter: int | None = "ALL") -> ScreenResult:
    """List books. Once the library passes 10 books, prefer the shelf landing
    page (`screen_shelves`) and let the user opt into the flat list with
    "Hepsini gör". When called with `shelf_filter` we list that shelf's books.

    Args:
        shelf_filter:
            - "ALL" (default sentinel): show every book regardless of shelf
            - None: show books with no shelf assigned
            - int: show books on this shelf
    """
    books = await data.list_books()
    if not books:
        return (
            "🏠 Ana › 📚 <b>Kitaplarım</b>\n\nHenüz kitabın yok.",
            KB([[BTN("➕ Yeni Kitap Ekle", "newbook:start")], _nav_row(False)]),
        )
    # Optional shelf filter
    if shelf_filter == "ALL":
        filtered = books
        header = f"🏠 Ana › 📚 <b>Kitaplarım</b>  ({len(books)})"
    elif shelf_filter is None:
        filtered = [b for b in books if not b.shelf_id]
        header = f"🏠 Ana › 📚 Kitaplarım › <b>Raflandırılmamış</b>  ({len(filtered)})"
    else:
        sh = await data.get_shelf(shelf_filter)
        filtered = [b for b in books if b.shelf_id == shelf_filter]
        label = f"{sh.icon} {sh.name}" if sh else "Raf"
        header = f"🏠 Ana › 📚 Kitaplarım › <b>{esc(label)}</b>  ({len(filtered)})"

    text = f"{header}\n\nDetay için birine bas:"
    kb = []
    # Big-library safety: cap inline list at ~25 to avoid Telegram payload limits
    show = filtered[:25]
    for b in show:
        status_icon = _STATUS_ICONS.get(b.status, "📖")
        emoji = getattr(b, "icon", None) or "📖"
        # If the user hasn't picked a custom icon, the book.icon is the same
        # 📖 as the READING-status icon → don't duplicate it.
        prefix = emoji if emoji != status_icon else status_icon
        suffix = f" — %{int(100 * (b.read_pages or 0) / b.total_pages)}" if b.total_pages else ""
        kb.append([BTN(
            f"{prefix} {b.short_code} · {b.title[:36]}{suffix}",
            _cb("book", b.id),
        )])
    if len(filtered) > len(show):
        kb.append([BTN(f"… +{len(filtered) - len(show)} kitap daha (rafa git)", "shelves")])
    # Useful actions
    if len(books) >= 5 and any(b.cover_url for b in books):
        kb.append([BTN("📸 Kapakları topluca göster", "covers_grid")])
    if len(books) >= 10:
        kb.append([BTN("📚 Raflar", "shelves")])
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
    emoji = getattr(book, "icon", None) or "📖"
    lines = [
        f"🏠 Ana › 📚 Kitaplarım › <b>{esc(book.title)}</b>",
        "",
        f"{emoji} <b>{esc(book.title)}</b>  ·  <code>{esc(book.short_code)}</code>",
    ]
    if book.author:
        lines.append(f"✍️ {esc(book.author)}")
    if getattr(book, "publisher", None):
        pub_line = f"🏢 {esc(book.publisher)}"
        if getattr(book, "publication_year", None):
            pub_line += f" · {book.publication_year}"
        lines.append(pub_line)
    if book.genre:
        sub = f" › {esc(book.subgenre)}" if book.subgenre else ""
        lines.append(f"🏷️ {esc(book.genre)}{sub}")
    if book.isbn:
        lines.append(f"🌐 ISBN: {esc(book.isbn)}")
    shelf = getattr(book, "shelf", None)
    if shelf:
        lines.append(f"📚 Raf: {esc(shelf.icon)} {esc(shelf.name)}")
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
    extra = dict(getattr(book, "extra_fields", None) or {})
    if extra:
        lines.append("")
        lines.append("<i>Kişisel alanlar:</i>")
        for k, v in extra.items():
            lines.append(f"  • <b>{esc(k)}</b>: {esc(str(v)[:80])}")
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
    # Cover-photo screen: do not pad (already tall with image)
    return "\n".join(lines), KB(kb, pad=False), book.cover_url


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
            f"• <code>{esc(sess.code)}</code> · <b>{esc(title)}</b> · {elapsed} dk · "
            f"s.{sess.start_page or '?'} · {len(sess.notes)} not"
        )
        # Per-session row: open / edit / delete
        kb.append([BTN(f"🟢 {sess.code} · {title[:24]}", _cb("session_open", sess.id))])
        kb.append([
            BTN("✏️ Sayfa düzelt",  _cb("session_edit", sess.id)),
            BTN("🗑️ Sil",           _cb("session_del", "ask", sess.id)),
        ])
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
        [BTN("✏️ Sayfa Düzelt", _cb("session_edit", session.id)),
         BTN("🗑️ Oturumu Sil", _cb("session_del", "ask", session.id))],
        [BTN("⏸️ Duraklat", _cb("pause", session.id)),
         BTN("⏹️ Bitir", _cb("end", session.id))],
        _nav_row(),
    ]
    return text, KB(kb)


async def screen_note_confirm(draft: dict[str, Any]) -> ScreenResult:
    """Note confirmation screen.

    v1.0.4: pulls user-defined custom categories from AppSettings and renders
    them as extra category-toggle buttons. The selected value is shown as
    `category_label` when active; `category` is left at the draft's enum value
    so storage stays consistent.
    """
    cats = [Category.QUOTE, Category.IDEA, Category.NEW_INFO,
            Category.WORD, Category.CONCEPT, Category.SUMMARY]
    current = draft["category"]
    current_label = draft.get("category_label") or ""
    short = draft.get("book_short_code") or ""
    title = draft.get("book_title") or ""
    settings = await data.get_settings()
    custom_cats = list(getattr(settings, "custom_categories", None) or [])

    # v1.0.5 (Madde 8): uzun transkript / tanım / açıklama'yı kısalt + buton
    transcript_full = draft.get("transcript") or ""
    transcript_view, t_trunc = _truncate_with_marker(transcript_full)
    parts = [
        f"🏠 Ana › 🟢 Okuyor › 📝 <b>Yeni Not</b>  ·  <code>{esc(short)}</code> {esc(title)}",
        "", "✅ Transkript:", f"<i>“{esc(transcript_view)}”</i>", "",
    ]
    if current_label:
        parts.append(f"🏷️ Kategori: <b>{esc(current_label)}</b> (özel)")
    else:
        parts.append(f"🤖 Kategori: <b>{esc(current)}</b>")
    parts.append(f"📄 Sayfa: s.{draft.get('page') or '—'}")
    show_full_button = t_trunc
    if draft.get("definition"):
        d_view, d_trunc = _truncate_with_marker(draft["definition"])
        parts += ["", f"📚 Otomatik tanım: {esc(d_view)}"]
        show_full_button = show_full_button or d_trunc
    if draft.get("explanation"):
        e_view, e_trunc = _truncate_with_marker(draft["explanation"])
        parts += ["", f"💡 Gemini açıklaması: {esc(e_view)}"]
        show_full_button = show_full_button or e_trunc

    def _cat_btn(c):
        active = (c.value == current and not current_label)
        return BTN(("✓ " if active else "") + c.value, _cb("voice", "cat", c.value))

    def _custom_btn(label):
        active = (label == current_label)
        return BTN(("✓ " if active else "") + f"🏷️ {label}",
                   _cb("voice", "ccat", label))

    kb: list[list[InlineKeyboardButton]] = [
        [_cat_btn(c) for c in cats[:3]],
        [_cat_btn(c) for c in cats[3:]],
    ]
    # Custom categories — 2 per row
    if custom_cats:
        row: list[InlineKeyboardButton] = []
        for lbl in custom_cats:
            row.append(_custom_btn(lbl))
            if len(row) == 2:
                kb.append(row); row = []
        if row:
            kb.append(row)
    if show_full_button:
        kb.append([BTN("📖 …devamını oku (taslakta tam metin)", "voice:full")])
    kb += [
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
    # v1.0.5 — render "Kaynak: …" footer as small italic dim text on its own
    # paragraph so the citation is visible but not noisy.
    body, sources = _split_answer_and_sources(answer)
    rendered = f"🤖 {esc(body)}"
    if sources:
        rendered += f"\n\n<i>📎 Kaynak: {esc(sources)}</i>"
    text = (
        f"🏠 Ana › ❓ <b>Soru › Cevap</b>  ·  {esc(book.title)}\n\n"
        f"❓ <i>{esc(question)}</i>\n\n{rendered}"
    )
    return text, KB([
        [BTN("📝 Not olarak kaydet", _cb("question", "save", book.id))],
        [BTN("🔁 Yeni soru", _cb("question", "ask", book.id))],
        [BTN("📖 Kitaba dön", _cb("book", book.id))],
    ])


def _split_answer_and_sources(answer: str) -> tuple[str, str]:
    """Pull the "Kaynak: …" footer off a Gemini answer.

    Returns (body, sources). If no source line is found, sources is "".
    """
    if not answer:
        return "", ""
    lines = answer.strip().splitlines()
    # Find last non-empty line; check if it starts with "Kaynak:" (case-insens)
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if not line:
            continue
        if line.lower().startswith("kaynak:") or line.lower().startswith("kaynaklar:"):
            sources = line.split(":", 1)[1].strip()
            body = "\n".join(lines[:i]).strip()
            return body, sources
        break  # Source line must be last; stop at first non-empty non-source
    return answer.strip(), ""


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


async def screen_shelves() -> ScreenResult:
    """Shelf landing page. Lists every shelf as a button + counts; also lets
    the user create a new shelf and jump to the un-shelved bucket / all books.
    """
    shelves = await data.list_shelves()
    books = await data.list_books()
    by_shelf: dict[int, int] = {}
    unshelved = 0
    for b in books:
        if b.shelf_id:
            by_shelf[b.shelf_id] = by_shelf.get(b.shelf_id, 0) + 1
        else:
            unshelved += 1

    lines = [
        f"🏠 Ana › 📚 Kitaplarım › <b>Raflar</b>  ({len(shelves)} raf)", "",
    ]
    if not shelves:
        lines.append("Henüz raf yok. Aşağıdan oluşturabilirsin.")
    else:
        for sh in shelves:
            n = by_shelf.get(sh.id, 0)
            lines.append(f"{sh.icon} <b>{esc(sh.name)}</b> — {n} kitap")
    if unshelved:
        lines.append(f"\n📦 Raflandırılmamış — {unshelved} kitap")

    kb: list[list[InlineKeyboardButton]] = []
    for sh in shelves:
        n = by_shelf.get(sh.id, 0)
        kb.append([BTN(f"{sh.icon} {sh.name} ({n})", _cb("shelf", sh.id))])
    if unshelved:
        kb.append([BTN(f"📦 Raflandırılmamış ({unshelved})", _cb("shelf", "none"))])
    kb.append([BTN("➕ Yeni raf oluştur", "shelf_new")])
    kb.append([BTN("📚 Hepsini gör (raf bağımsız)", "books_all")])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


def screen_help() -> ScreenResult:
    text = (
        "🏠 Ana › ❓ <b>Yardım</b>\n\n"
        "<b>Bot ne yapıyor?</b>\n"
        "Kitap okurken sesli/yazılı/fotoğraflı not tutman, "
        "kitap istatistiklerini takip etmen ve okuma günlüğünü "
        "PDF olarak almak için bir asistan.\n\n"
        "<b>Hızlı komutlar (/ menüsü)</b>\n"
        "• <b>/yeni</b> — kitap ekle (yazı, ISBN ya da kapak fotoğrafı)\n"
        "• <b>/oturum</b> — yeni okuma oturumu başlat\n"
        "• <b>/oturumlar</b> — açık oturumlarını gör, düzenle ya da sil\n"
        "• <b>/kitaplar</b> — kütüphanen\n"
        "• <b>/ara</b> — notlarında ara (FTS)\n"
        "• <b>/sozluk</b> — Kelime + Kavram notların\n"
        "• <b>/alintilar</b> — Alıntı kategorili notlar\n"
        "• <b>/istatistik</b> — okuma istatistikleri\n"
        "• <b>/ayarlar</b> — bot davranışı (kategori önerisi, hatırlatma vb.)\n\n"
        "<b>Açık oturumdayken</b>\n"
        "🎤 ses → not  ·  📷 sayfa fotoğrafı → OCR  ·  ✍️ yazı → not\n\n"
        "<b>Kitap ekleme yolları</b>\n"
        "🔢 ISBN — sayıyı yazarsın, kapak/yazar/sayfa otomatik\n"
        "📷 Kapak fotoğrafı — ön/arka kapağı çekersin, Gemini ISBN/başlık/yazarı okur\n"
        "✍️ Elle — sadece başlık"
    )
    return text, KB([_nav_row()])


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


# v1.0.2 — "...devamını oku" sınırı. ~10 satır / 500 karakter civarı
_NOTE_PREVIEW_CHARS = 500
_NOTE_PREVIEW_LINES = 10


# v1.0.5 — Photo+caption Gemini answer formatter.
# Gemini PROMPT_PHOTO_QUESTION'a göre şu formatta cevap döner:
#   "OCR alıntısı"
#
#   TANIM: ...
#   CEVAP: ...
# Bunu HTML olarak render et: ilk tırnaklı satır italic blok, ETIKET: satırları
# bold etiketli paragraflar. Diğer satırlar olduğu gibi geçer.
_PHOTO_LABEL_PREFIXES = (
    "TANIM:", "CEVAP:", "ÖZET:", "OZET:", "BAĞLAM:", "BAGLAM:",
    "AÇIKLAMA:", "ACIKLAMA:", "İLİŞKİ:", "ILISKI:", "NOT:",
)


def _format_photo_answer_html(raw: str) -> str:
    """Render Gemini's photo-question reply as Telegram HTML.

    Treats the first quoted block (lines wrapped in " or ") as italic OCR;
    subsequent "ETIKET: …" lines as <b>ETIKET:</b> normal text.
    """
    if not raw:
        return ""
    raw = raw.strip()
    parts: list[str] = []
    paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
    first_done = False
    for para in paragraphs:
        if not first_done and (para.startswith('"') or para.startswith('“')):
            # OCR quote — italic
            text = para.strip('"').strip("“”")
            parts.append(f"<i>“{esc(text)}”</i>")
            first_done = True
            continue
        # Detect "ETIKET: …" lines (case-insensitive)
        up = para.upper()
        label_match = None
        for lab in _PHOTO_LABEL_PREFIXES:
            if up.startswith(lab):
                label_match = lab
                break
        if label_match:
            value = para[len(label_match):].strip()
            parts.append(f"<b>{esc(label_match)}</b> {esc(value)}")
        else:
            # Just normal paragraph
            parts.append(esc(para))
        first_done = True
    return "\n\n".join(parts)


def _truncate_with_marker(text: str, *, chars: int = _NOTE_PREVIEW_CHARS,
                          lines: int = _NOTE_PREVIEW_LINES) -> tuple[str, bool]:
    """Return (display_text, was_truncated). Cuts at the smaller of char or
    line limit and appends an ellipsis. Used for note transcripts in lists
    and detail screens — full text always fetchable via 'note_full' callback.
    """
    if not text:
        return "", False
    line_split = text.split("\n")
    truncated_by_line = False
    if len(line_split) > lines:
        text_l = "\n".join(line_split[:lines])
        truncated_by_line = True
    else:
        text_l = text
    if len(text_l) > chars:
        text_l = text_l[:chars]
        return text_l.rstrip() + "…", True
    return text_l + ("…" if truncated_by_line else ""), truncated_by_line


async def screen_note_detail(
    note_id: int, *, full: bool = False,
) -> tuple[str, InlineKeyboardMarkup, str | None]:
    """v1.0.5: returns (text, kb, photo_file_id) so callers can render the
    attached photo (if any) above the caption.
    """
    note = await data.get_note(note_id)
    if not note:
        return "Not bulunamadı.", KB([_nav_row()]), None
    book = await data.get_book(note.book_id)
    title = book.title if book else "?"
    ts = to_local(note.created_at).strftime("%d %b %Y · %H:%M") if note.created_at else ""
    if full:
        body = note.transcript or ""
        truncated = False
    else:
        body, truncated = _truncate_with_marker(note.transcript or "")
    lines = [
        f"🏠 Ana › 📚 {esc(title)} › 📝 <b>Not</b>  ·  <code>{esc(note.code)}</code>",
        "",
        f"📅 {esc(ts)}",
        f"🏷️ {esc(note.category.value)}  ·  📄 s.{note.page or '—'}",
        "",
        f"<i>“{esc(body)}”</i>",
    ]
    if note.definition:
        d_body, d_trunc = _truncate_with_marker(note.definition) if not full else (note.definition, False)
        lines += ["", f"📚 Tanım: {esc(d_body)}"]
        truncated = truncated or d_trunc
    if note.explanation:
        e_body, e_trunc = _truncate_with_marker(note.explanation) if not full else (note.explanation, False)
        lines += ["", f"💡 Açıklama: {esc(e_body)}"]
        truncated = truncated or e_trunc
    if note.is_favorite:
        lines.append("\n⭐ Favori")

    kb_rows: list[list[InlineKeyboardButton]] = []
    if truncated:
        kb_rows.append([BTN("📖 …devamını oku", _cb("note_full", note.id))])
    elif full:
        kb_rows.append([BTN("⤴️ Kısalt", _cb("note", note.id))])
    kb_rows += [
        [BTN("⭐ Favoriden çıkar" if note.is_favorite else "⭐ Favoriye ekle",
             _cb("note_fav", note.id))],
        [BTN("✏️ Transkripti düzelt", _cb("note_edit", note.id)),
         BTN("📄 Sayfa değiştir", _cb("note_page", note.id))],
        [BTN("📤 Paylaş (görsel/PDF)", _cb("note_share", note.id))],
        [BTN("🗑️ Notu sil", _cb("note_del", note.id))],
        _nav_row(),
    ]
    # Photo present? Tell the caller — _send_screen will use it.
    file_id = getattr(note, "photo_file_id", None)
    return "\n".join(lines), KB(kb_rows, pad=False if file_id else True), file_id


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


# v1.0.2: page size for "load more" lists (small enough to fit one phone screen)
_LIST_PAGE_SIZE = 5


async def screen_search_results(query: str, limit: int = _LIST_PAGE_SIZE) -> ScreenResult:
    results = await data.search_notes(query)
    total = len(results)
    lines = [f"🏠 Ana › 🔍 <b>Ara</b>: <code>{esc(query)}</code>", ""]
    kb = []
    if total == 0:
        lines.append("<i>(Sonuç yok.)</i>")
    else:
        showing = min(limit, total)
        lines.append(f"{total} sonuç · {showing} gösteriliyor\n")
        for note, book in results[:limit]:
            snip = note.transcript[:80] + ("…" if len(note.transcript) > 80 else "")
            lines.append(
                f"<code>{esc(note.code)}</code> · {esc(book.title)}\n  <i>{esc(snip)}</i>"
            )
            kb.append([BTN(f"{note.code} · {note.category.value}", _cb("note", note.id))])
        if showing < total:
            kb.append([BTN(
                f"⬇️ {min(_LIST_PAGE_SIZE, total - showing)} sonuç daha göster",
                _cb("search", "more", limit + _LIST_PAGE_SIZE, query[:64]),
            )])
    kb.append([BTN("🔁 Yeni arama", "search:start")])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


async def screen_glossary(limit: int = _LIST_PAGE_SIZE) -> ScreenResult:
    entries = await data.list_glossary()
    total = len(entries)
    lines = [f"🏠 Ana › 📖 <b>Sözlük</b>  ({total} terim)", ""]
    if total == 0:
        lines.append("<i>(Henüz Kelime/Kavram notun yok.)</i>")
        return "\n".join(lines), KB([_nav_row()])
    showing = min(limit, total)
    lines.append(f"{showing}/{total} gösteriliyor\n")
    kb = []
    for note, book in entries[:limit]:
        marker = "🔤" if note.category == Category.WORD else "🧠"
        term = note.transcript[:40]
        lines.append(f"{marker} <b>{esc(term)}</b>  ·  <i>{esc(book.short_code)}</i>")
        if note.definition:
            lines.append(f"   {esc(note.definition[:120])}")
        kb.append([BTN(f"{marker} {term[:30]}", _cb("note", note.id))])
    if showing < total:
        kb.append([BTN(
            f"⬇️ {min(_LIST_PAGE_SIZE, total - showing)} terim daha göster",
            _cb("glossary", "more", limit + _LIST_PAGE_SIZE),
        )])
    kb.append(_nav_row())
    return "\n".join(lines), KB(kb)


async def screen_quotes(
    favorites_only: bool = False, limit: int = _LIST_PAGE_SIZE,
) -> ScreenResult:
    entries = await data.list_quotes(favorites_only=favorites_only)
    total = len(entries)
    title = "💬 Favori Alıntılar" if favorites_only else "💬 Tüm Alıntılar"
    lines = [f"🏠 Ana › <b>{title}</b>  ({total})", ""]
    if total == 0:
        lines.append("<i>(Henüz alıntı yok.)</i>")
        return "\n".join(lines), KB([_nav_row()])
    showing = min(limit, total)
    lines.append(f"{showing}/{total} gösteriliyor\n")
    kb = []
    for note, book in entries[:limit]:
        star = "⭐ " if note.is_favorite else ""
        lines.append(
            f"{star}<code>{esc(note.code)}</code> · {esc(book.title)}\n"
            f"  <i>“{esc(note.transcript[:140])}”</i>"
        )
        kb.append([BTN(f"{star}{note.code} · {book.short_code}", _cb("note", note.id))])
    if showing < total:
        scope = "fav" if favorites_only else "all"
        kb.append([BTN(
            f"⬇️ {min(_LIST_PAGE_SIZE, total - showing)} alıntı daha göster",
            _cb("quotes", scope, "more", limit + _LIST_PAGE_SIZE),
        )])
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
    text = (
        "🏠 Ana › ➕ <b>Yeni Kitap</b>\n\n"
        "Yeni kitabı 3 farklı yoldan ekleyebilirsin:\n\n"
        "• <b>🔢 ISBN ile</b> — sayıyı yazarsın, kapak/yazar/sayfa otomatik gelir\n"
        "• <b>📷 Kapak fotoğrafıyla</b> — kitabın ön ya da arka kapağını çek, Gemini ISBN/başlık/yazarı okur, geri kalanı doldurur\n"
        "• <b>✍️ Elle</b> — sadece başlık girersin, metadata otomatik yok"
    )
    return text, KB([
        [BTN("🔢 ISBN ile", "newbook:isbn")],
        [BTN("📷 Kapak fotoğrafıyla", "newbook:cover")],
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
    # Preview is often shown with a cover photo → keep KB compact
    return "\n".join(lines), KB([
        [BTN("✅ Otomatik kod ile kaydet", "newbook:save")],
        [BTN("🔤 Kısa kodu ben gireyim", "newbook:codefirst")],
        [BTN("❌ İptal", "main")],
    ], pad=False)


# ────────────────────────── /start command ──────────────────────────


@_safe_handler
async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    cid = _chat_id(update)
    logger.info("bot.command_start", user_id=user_id, chat_id=cid)
    if cid is not None:
        await _clear(cid, "awaiting", "draft_note", "mode", "question_book_id",
                     "newbook_draft", "last_qa", "finish_book_id", "pending_input")
    # 1) Install the persistent quick-action reply keyboard (Madde 5).
    # We send it on a tiny separate message because a single message can't
    # carry both an inline keyboard and a reply keyboard.
    try:
        await update.message.reply_text(
            "⚡ Hızlı butonlar aşağıda hep açık olacak.",
            reply_markup=_quick_keyboard(),
        )
    except Exception as e:
        logger.debug("bot.start.quick_kb_failed", error=str(e))
    # 2) Inline main menu
    text, kb = await screen_main()
    sent = await update.message.reply_text(
        text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )
    if cid is not None and sent is not None:
        await _track_last_menu(cid, sent.message_id)


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
    # v1.0.5 (Madde 9) — tek aktif menü: önceki menüyü user input geldiğinde sil
    await _delete_previous_menu(cid, context)

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
    try:
        async with _Progress(msg, "🔄 Ses metne dönüştürülüyor… (Gemini)"):
            transcript = await _transcribe_voice_msg(msg)
            settings = await data.get_settings()
            category = (
                await ai.suggest_category(transcript)
                if settings.auto_categorize else Category.NEW_INFO
            )
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
    text, kb = await screen_note_confirm(draft)
    await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@_safe_handler
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = _chat_id(update)
    msg = update.message
    logger.info("bot.photo.received", user_id=update.effective_user.id)
    # v1.0.5 (Madde 9) — tek aktif menü
    await _delete_previous_menu(cid, context)

    # Route 1: newbook cover photo (mode set by "Yeni Kitap → Kapak fotoğrafıyla")
    mode = await _get(cid, "mode") if cid else None
    if isinstance(mode, dict) and mode.get("name") == "newbook_cover":
        await _process_newbook_cover(update, context)
        return

    # Route 2: photo of a page. Three sub-routes:
    #   2a — caption present → Q&A about the page (Gemini reads + answers)
    #   2b — no caption + highlights detected → save highlight as note
    #   2c — no caption + no highlights → orphan photo (user writes mandatory note)
    session = await _resolve_session_for_input(update, context)
    if session is None:
        return

    file_id = msg.photo[-1].file_id if msg.photo else None
    caption = (msg.caption or "").strip()
    book = await data.get_book(session.book_id)

    # ── 2a: caption = soru, fotoğraf = sayfa ─────────────────────
    if caption:
        try:
            async with _Progress(msg, "🔄 Sayfa okunup soruna cevap üretiliyor…"):
                photo_file = await msg.photo[-1].get_file()
                image_bytes = await photo_file.download_as_bytearray()
                answer = await ai.answer_about_image(
                    bytes(image_bytes), caption,
                    book_title=book.title if book else "(bilinmiyor)",
                    book_author=(book.author if book and book.author else "(bilinmiyor)"),
                )
        except Exception as e:
            logger.error("bot.photo.qa_failed", error=str(e), exc_info=True)
            await _err_reply(msg, e, "Sayfa-soru işlenirken hata")
            return
        # Save Q&A as a note so the user has a record
        try:
            await data.add_note(
                book_id=session.book_id, session_id=session.id,
                category=Category.CONCEPT, page=None,
                transcript=f"Talimat: {caption}\n\n{answer}",
                from_qa=True, photo_file_id=file_id,
            )
        except Exception as e:
            logger.warning("bot.photo.qa_save_failed", error=str(e))
        # Format answer (Madde 3): first quoted line italic, label rows bold.
        formatted = _format_photo_answer_html(answer)
        # Post-save action buttons so the user always knows where to go next
        post_kb = KB([
            [BTN("🟢 Aktif Oturuma Dön", _cb("session_open", session.id))],
            [BTN("📝 Bu Notu Aç", _cb("notes", session.book_id, 0)),
             BTN("📷 Yeni fotoğraf at", "noop")],
            [BTN("🏠 Ana Menü", "main")],
        ])
        # Cap visible response (Madde 2 — devamını oku yayılımı).
        body_short, _was_trunc = _truncate_with_marker(formatted, chars=900, lines=14)
        await msg.reply_text(
            f"📷 <b>Fotoğraf + talimat</b>  ·  "
            f"<code>{esc(book.short_code if book else '')}</code> "
            f"{esc(book.title if book else '')}\n\n"
            f"❓ <i>{esc(caption)}</i>\n\n"
            f"{body_short}\n\n"
            f"<i>Not olarak kaydedildi. Bir sonraki adımı seç:</i>",
            reply_markup=post_kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    # ── 2b/2c: caption yok → vurgu modu OCR ───────────────────────
    # Davranış değişikliğini ilk kez gönderirken kısa bilgilendir.
    show_hint = False
    if cid is not None:
        seen = await _get(cid, "photo_hint_shown")
        if not seen:
            show_hint = True
            await _set(cid, "photo_hint_shown", True, ttl_s=30 * 24 * 3600)

    try:
        async with _Progress(msg, "🔄 Fotoğraf işleniyor… (vurguları arıyorum)"):
            photo_file = await msg.photo[-1].get_file()
            image_bytes = await photo_file.download_as_bytearray()
            transcript, detected_page = await ai.ocr_image(
                bytes(image_bytes), "image/jpeg", mode="highlight",
            )
            settings = await data.get_settings()
            no_highlight = (
                not (transcript or "").strip()
                or "VURGU_YOK" in (transcript or "").upper()
            )
            if no_highlight:
                category = Category.IDEA  # placeholder; orphan flow
                transcript = ""
            else:
                category = (
                    await ai.suggest_category(transcript)
                    if settings.auto_categorize else Category.QUOTE
                )
    except Exception as e:
        logger.error("bot.photo.failed", error=str(e), exc_info=True)
        await _err_reply(msg, e, "Fotoğraf işlenirken hata")
        return

    hint_text = (
        "\n\n💡 <i>İpucu: bir sonraki sefer fotoğrafa caption (yazı) eklersen "
        "sayfaya bakıp sorunu cevaplarım. Caption yoksa sadece <b>altı çizili / "
        "vurgulanmış</b> kısımları çıkarırım, tam sayfayı değil.</i>"
    ) if show_hint else ""

    # ── 2c: vurgu yok ─────────────────────────────────────────────
    if no_highlight:
        if cid is not None:
            await _set(cid, "awaiting", {
                "type": "orphan_photo_note",
                "session_id": session.id,
                "book_id": session.book_id,
                "photo_file_id": file_id,
                "page": detected_page,
            })
        await msg.reply_text(
            f"📷 <b>Sayfada vurgu/altı çizili bir parça bulamadım.</b>  ·  "
            f"<code>{esc(book.short_code if book else '')}</code> "
            f"{esc(book.title if book else '')}\n\n"
            "Bu görselle ilgili bir not yaz (zorunlu). Yazdığını fotoğrafla "
            "birlikte kaydederim; PDF günlüğüne 'sahne' olarak eklenir."
            f"{hint_text}",
            reply_markup=KB([
                [BTN("❌ Vazgeç (fotoğrafı atla)", "voice:cancel")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return

    # ── 2b: vurgular var → not draft ──────────────────────────────
    draft = _build_draft(session, book, transcript, category, detected_page)
    draft["photo_file_id"] = file_id
    if cid is not None:
        await _set(cid, "draft_note", draft)

    if detected_page is None:
        if cid is not None:
            await _set(cid, "awaiting", {"type": "draft_page"})
        text = (
            f"📷 <b>Vurgular okundu</b>  ·  <code>{esc(book.short_code if book else '')}</code> "
            f"{esc(book.title if book else '')}\n\n"
            "Sayfa numarasını fotoğrafta bulamadım. Lütfen sayfa numarasını yaz "
            "(sadece sayı). Sonra notu onaylarsın."
            f"{hint_text}"
        )
        await msg.reply_text(
            text, reply_markup=KB([[BTN("📄 Sayfa belirsiz, atla", "voice:skippage")]]),
            parse_mode=ParseMode.HTML,
        )
        return

    text, kb = await screen_note_confirm(draft)
    if hint_text:
        text = text + hint_text
    await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@_safe_handler
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = _chat_id(update)
    msg = update.message
    text = (msg.text or "").strip()
    logger.info("bot.text.received", user_id=update.effective_user.id, length=len(text))
    # v1.0.5 (Madde 9) — tek aktif menü
    await _delete_previous_menu(cid, context)

    # v1.0.2 — quick reply-keyboard buttons (Madde 5).
    # We intercept these BEFORE the question/awaiting/note routes so the user
    # can always escape into a top-level menu.
    if text in _QUICK_LABELS:
        if cid is not None:
            # Quick actions interrupt any pending awaiting/mode state
            await _clear(cid, "awaiting", "mode")
        if text == _QUICK_OTURUMLAR:
            await handle_callback_proxy(update, context, "open_sessions")
        elif text == _QUICK_KITAPLAR:
            await handle_callback_proxy(update, context, "books")
        elif text == _QUICK_YENI:
            await handle_callback_proxy(update, context, "newbook:start")
        elif text == _QUICK_BITIR:
            # End the *current default* session; if none, fall back to chooser
            open_sessions = await data.list_open_sessions()
            if not open_sessions:
                await msg.reply_text(
                    "🟡 Bitirebileceğin açık oturum yok.",
                    reply_markup=_quick_keyboard(),
                )
                return
            if len(open_sessions) == 1:
                target = open_sessions[0]
            else:
                default_id = await _get(cid, "default_session") if cid else None
                target = next(
                    (s for s in open_sessions if s.id == default_id),
                    open_sessions[0],
                )
            # Ask for end page
            if cid is not None:
                await _set(cid, "awaiting", {
                    "type": "end_page",
                    "session_id": target.id,
                    "book_id": target.book_id,
                })
            await msg.reply_text(
                f"⏹️ <b>Oturumu Bitir</b>  ·  <code>{esc(target.code)}</code>\n\n"
                "Şu an hangi sayfadasın? Numarayı yaz.",
                parse_mode=ParseMode.HTML, reply_markup=_quick_keyboard(),
            )
        return

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


async def _process_newbook_cover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo of a book cover → extract isbn/title/author via Gemini → fetch
    metadata from Google Books → show preview screen.
    """
    msg = update.message
    cid = _chat_id(update)
    try:
        async with _Progress(msg, "🔄 Kapak okunuyor… (Gemini ISBN/başlık tanıma)"):
            photo_file = await msg.photo[-1].get_file()
            image_bytes = await photo_file.download_as_bytearray()
            extracted = await ai.extract_book_from_cover(bytes(image_bytes), "image/jpeg")
    except Exception as e:
        logger.error("bot.newbook_cover.failed", error=str(e), exc_info=True)
        await _err_reply(msg, e, "Kapak okunurken hata")
        return

    raw_isbn = extracted.get("isbn")
    isbn = data.normalize_isbn(raw_isbn) if raw_isbn else None
    title = extracted.get("title")
    author = extracted.get("author")

    metadata: dict | None = None
    lookup_label = ""
    if isbn:
        try:
            async with _Progress(msg, "🔄 ISBN aranıyor… (Google Books → Open Library)"):
                metadata = await data.lookup_book_metadata(isbn)
            lookup_label = f"ISBN <code>{esc(isbn)}</code>"
        except Exception as e:
            logger.warning("bot.newbook_cover.isbn_lookup_failed", error=str(e))
    if metadata is None and (title or author):
        try:
            async with _Progress(msg, "🔄 Başlık + yazar aranıyor… (Google Books → Open Library)"):
                metadata = await data.lookup_book_by_title_author(title, author)
            lookup_label = f"Başlık: <b>{esc(title or '?')}</b>"
            if author:
                lookup_label += f", Yazar: <b>{esc(author)}</b>"
        except Exception as e:
            logger.warning("bot.newbook_cover.title_lookup_failed", error=str(e))

    if cid is not None:
        # Leave newbook_cover mode regardless of outcome
        await _clear(cid, "mode")

    if metadata is None:
        # Couldn't find — let the user add manually with whatever we did parse
        fallback_title = title or "(başlıksız)"
        await msg.reply_text(
            f"⚠️ Kapaktan bilgi çıkarıldı ama ne Google Books'ta ne de Open Library'de "
            f"eşleşme bulunamadı.\n\n"
            f"Bulunanlar:\n"
            f"• ISBN: <code>{esc(isbn) if isbn else '—'}</code>\n"
            f"• Başlık: <b>{esc(title) if title else '—'}</b>\n"
            f"• Yazar: <b>{esc(author) if author else '—'}</b>\n\n"
            f"Ne yapmak istersin?",
            reply_markup=KB([
                [BTN("🔢 ISBN'i elle gir", "newbook:isbn")],
                [BTN(f"✍️ Elle ekle: {fallback_title[:25]}", "newbook:manual")],
                [BTN("📷 Yeniden kapak çek", "newbook:cover")],
                _nav_row(),
            ]),
            parse_mode=ParseMode.HTML,
        )
        return

    # Show preview screen (same as ISBN path)
    if cid is not None:
        await _set(cid, "newbook_draft", metadata)
    s_text, kb = screen_newbook_preview(metadata)
    s_text = f"{s_text}\n\n<i>Eşleşme: {lookup_label}</i>"
    if metadata.get("cover_url"):
        try:
            await msg.reply_photo(
                photo=metadata["cover_url"], caption=s_text,
                reply_markup=kb, parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            logger.warning("bot.newbook_cover.cover_send_failed", error=str(e))
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _process_text_into_note(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    session: Session, text: str,
) -> None:
    msg = update.message
    cid = _chat_id(update)
    settings = await data.get_settings()
    try:
        if settings.auto_categorize:
            async with _Progress(msg, "🔄 Not analiz ediliyor… (kategori + tanım)"):
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
    s_text, kb = await screen_note_confirm(draft)
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ────────────────────────── input sub-routines ──────────────────────────


async def _handle_question_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    voice: bool, book_id: int | None,
) -> None:
    msg = update.message
    cid = _chat_id(update)
    if voice:
        try:
            async with _Progress(msg, "🔄 Sesli soru metne dönüştürülüyor…"):
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

    try:
        async with _Progress(msg, "🔄 Gemini sorunu yanıtlıyor…"):
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
        try:
            async with _Progress(msg, "🔄 Özet sesi metne dönüştürülüyor…"):
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
        try:
            async with _Progress(msg, "🔄 Yorum sesi metne dönüştürülüyor…"):
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
        s_text, kb = await screen_note_confirm(draft)
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
        s_text, kb = await screen_note_confirm(draft)
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
    raw = text
    isbn_clean = data.normalize_isbn(raw)
    if not isbn_clean:
        await msg.reply_text(
            f"⚠️ <code>{esc(raw)}</code> geçerli bir ISBN gibi durmuyor.\n\n"
            "📐 <b>Beklenen format</b>\n"
            "• 13 haneli (<code>978…</code> ya da <code>979…</code>) veya 10 haneli\n"
            "• Tire (-) ve boşluk olabilir, otomatik temizlenir\n\n"
            "Tekrar dene ya da kapağı çek:",
            reply_markup=KB([
                [BTN("📷 Kapak fotoğrafıyla devam et", "newbook:cover")],
                _nav_row(),
            ]),
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        async with _Progress(msg, "🔄 ISBN aranıyor… (Google Books → Open Library)"):
            metadata = await data.lookup_book_metadata(isbn_clean)
    except Exception as e:
        await _err_reply(msg, e, "Aranamadı")
        return
    if metadata is None:
        await msg.reply_text(
            f"⚠️ ISBN <code>{esc(isbn_clean)}</code> için kitap bulunamadı "
            "(Google Books ve Open Library denendi).\n\n"
            "Ne yapmak istersin?",
            reply_markup=KB([
                [BTN("📷 Kapak fotoğrafıyla dene", "newbook:cover")],
                [BTN("✍️ Elle ekle (sadece başlık)", "newbook:manual")],
                [BTN("🔢 Başka ISBN gir", "newbook:isbn")],
                _nav_row(),
            ]),
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
    text = (text or "").strip()
    text_fields = {"title", "author", "genre", "subgenre", "isbn", "publisher",
                   "bought_from", "personal_note"}
    int_fields = {"total_pages", "price_tl", "publication_year"}
    if field == "tags":
        await data.update_book(book_id, tags=[t.strip() for t in text.split(",") if t.strip()])
    elif field == "short_code":
        norm = data.normalize_short_code(text)
        if not norm:
            await msg.reply_text("⚠️ Geçersiz kod (2-8 alfanumerik).")
            return
        await data.update_book(book_id, short_code=norm)
    elif field in int_fields:
        try:
            val = int("".join(c for c in text if c.isdigit() or c == "-"))
        except ValueError:
            await msg.reply_text("Geçerli bir sayı gir.")
            return
        await data.update_book(book_id, **{field: val})
    elif field in text_fields:
        await data.update_book(book_id, **{field: text})
    else:
        await msg.reply_text(f"⚠️ Bilinmeyen alan: {field}")
        return
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
    s_text, kb, file_id = await screen_note_detail(note_id)
    if file_id:
        await msg.reply_photo(
            photo=file_id, caption=s_text[:1000],
            reply_markup=kb, parse_mode=ParseMode.HTML,
        )
    else:
        await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_session_edit_page(update, context, text, awaiting):
    """User typed a new start_page for an active session."""
    msg = update.message
    cid = _chat_id(update)
    try:
        page = int(text)
    except ValueError:
        await msg.reply_text("Geçerli bir sayı gir.")
        return
    sess_id = awaiting.get("session_id")
    if not sess_id:
        return
    # Update the start_page directly (sync via to_thread)
    import asyncio as _asyncio
    def _upd():
        with data.db_session() as s:
            sess = s.get(data.Session, sess_id)
            if sess is not None:
                sess.start_page = page
    await _asyncio.to_thread(_upd)
    data.mark_dirty()
    if cid is not None:
        await _clear(cid, "awaiting")
    sess = await data.get_session(sess_id)
    s_text, kb = await screen_active_session(sess)
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_book_extra_add(update, context, text, awaiting):
    """User typed a NEW custom field name. Now we ask for the value."""
    msg = update.message
    cid = _chat_id(update)
    name = text.strip()
    if not name or len(name) > 60:
        await msg.reply_text("Alan adı boş ya da çok uzun (en fazla 60 karakter).")
        return
    book_id = awaiting.get("book_id")
    if cid is not None:
        await _set(cid, "awaiting", {
            "type": "book_extra_value",
            "book_id": book_id,
            "field_name": name,
        })
    await msg.reply_text(
        f"📝 <b>{esc(name)}</b> için değeri yaz ve gönder.",
        parse_mode=ParseMode.HTML,
    )


async def _await_book_extra_value(update, context, text, awaiting):
    """User typed a value for the named extra field."""
    msg = update.message
    cid = _chat_id(update)
    book_id = awaiting.get("book_id")
    name = awaiting.get("field_name")
    if not (book_id and name):
        return
    await data.set_book_extra_field(book_id, name, text.strip()[:500])
    if cid is not None:
        await _clear(cid, "awaiting")
    await msg.reply_text(
        f"✅ <b>{esc(name)}</b> kaydedildi.", parse_mode=ParseMode.HTML,
    )
    s_text, kb, cover = await screen_book_detail(book_id)
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_book_icon(update, context, text, awaiting):
    """User typed an emoji to use as the book icon."""
    msg = update.message
    cid = _chat_id(update)
    book_id = awaiting.get("book_id")
    icon = text.strip()[:8] or "📖"
    await data.update_book(book_id, icon=icon)
    if cid is not None:
        await _clear(cid, "awaiting")
    await msg.reply_text(f"✅ İkon güncellendi: {icon}")
    s_text, kb, cover = await screen_book_detail(book_id)
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_new_category_name(update, context, text, awaiting):
    """User typed a name for a new custom note category."""
    msg = update.message
    cid = _chat_id(update)
    name = text.strip()
    if not (2 <= len(name) <= 40):
        await msg.reply_text("⚠️ Kategori adı 2-40 karakter arası olmalı. Tekrar dene.")
        return
    settings = await data.get_settings()
    cats = list(getattr(settings, "custom_categories", None) or [])
    if name in cats:
        await msg.reply_text(f"<b>{esc(name)}</b> zaten var.", parse_mode=ParseMode.HTML)
    else:
        cats.append(name)
        await data.update_settings(custom_categories=cats)
        await msg.reply_text(
            f"✅ <b>{esc(name)}</b> kategorisi eklendi. Yeni not eklerken seçilebilir.",
            parse_mode=ParseMode.HTML,
        )
    if cid is not None:
        await _clear(cid, "awaiting")
    s_text, kb = await screen_notes_hub()
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_shelf_new_name(update, context, text, awaiting):
    """User typed a name for a new shelf."""
    msg = update.message
    cid = _chat_id(update)
    name = text.strip()
    if not name:
        await msg.reply_text("Raf adı boş olamaz.")
        return
    shelf = await data.create_shelf(name)
    if cid is not None:
        await _clear(cid, "awaiting")
    await msg.reply_text(
        f"✅ <b>{esc(shelf.icon)} {esc(shelf.name)}</b> rafı oluşturuldu.",
        parse_mode=ParseMode.HTML,
    )
    s_text, kb = await screen_shelves()
    await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _await_orphan_photo_note(update, context, text, awaiting):
    """User typed a description for a photo whose OCR returned nothing.

    We save the photo+text as an IDEA note flagged `is_orphan_photo=True` so
    the PDF can show a 📷-tagged "scene" entry rather than treating it as a
    page transcript.
    """
    msg = update.message
    cid = _chat_id(update)
    if not text.strip():
        await msg.reply_text("Lütfen kısa da olsa bir not yaz (zorunlu).")
        return
    book_id = awaiting.get("book_id")
    session_id = awaiting.get("session_id")
    photo_file_id = awaiting.get("photo_file_id")
    page = awaiting.get("page")
    try:
        await data.add_note(
            book_id=book_id, session_id=session_id,
            category=Category.IDEA, page=page,
            transcript=text.strip(),
            photo_file_id=photo_file_id, is_orphan_photo=True,
        )
    except Exception as e:
        logger.error("bot.orphan_photo.save_failed", error=str(e), exc_info=True)
        await _err_reply(msg, e, "Görsel notu kaydedilemedi")
        return
    if cid is not None:
        await _clear(cid, "awaiting")
    await msg.reply_text(
        "✅ Görsel + not kaydedildi (📷 sahne olarak).",
        parse_mode=ParseMode.HTML,
    )
    sess = await data.get_session(session_id) if session_id else None
    if sess and sess.ended_at is None:
        s_text, kb = await screen_active_session(sess)
        await msg.reply_text(s_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        m_text, m_kb = await screen_main()
        await msg.reply_text(m_text, reply_markup=m_kb, parse_mode=ParseMode.HTML)


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
    "session_edit_page":  _await_session_edit_page,
    "orphan_photo_note":  _await_orphan_photo_note,
    "book_extra_add":     _await_book_extra_add,
    "book_extra_value":   _await_book_extra_value,
    "book_icon":          _await_book_icon,
    "shelf_new_name":     _await_shelf_new_name,
    "new_category_name":  _await_new_category_name,
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
    # v1.0.5 (Madde 8) — Acknowledge the tap IMMEDIATELY with a toast.
    # This is the SAME single query.answer() the framework requires; we just
    # moved it from _send_screen's end to here, with text added. No extra API
    # call.  Telegram shows a small popup so the user sees "✅ tıklandı"
    # while the handler runs.
    try:
        await query.answer(text="⏳ Hazırlanıyor…", cache_time=0)
    except Exception as e:
        # Already answered or expired — fine.
        logger.debug("bot.callback.answer_failed", error=str(e))
    await handler(update, context, args)


# ── individual callback handlers ──

async def _cb_noop(update, context, args):
    # handle_callback already called query.answer() — nothing to do here.
    pass


async def _cb_main(update, context, args):
    text, kb = await screen_main()
    await _send_screen(update, context, text, kb)


async def _cb_books(update, context, args):
    # 10+ kitabı olan kullanıcıya raf landing page'i öner; yoksa düz liste
    books = await data.list_books()
    if len(books) >= 10:
        text, kb = await screen_shelves()
    else:
        text, kb = await screen_book_list()
    await _send_screen(update, context, text, kb)


async def _cb_books_all(update, context, args):
    """Flat all-books list, bypassing the shelf landing page."""
    text, kb = await screen_book_list(shelf_filter="ALL")
    await _send_screen(update, context, text, kb)


async def _cb_shelves(update, context, args):
    text, kb = await screen_shelves()
    await _send_screen(update, context, text, kb)


async def _cb_shelf(update, context, args):
    if not args:
        text, kb = await screen_shelves()
    elif args[0] == "none":
        text, kb = await screen_book_list(shelf_filter=None)
    else:
        text, kb = await screen_book_list(shelf_filter=int(args[0]))
    await _send_screen(update, context, text, kb)


async def _cb_shelf_new(update, context, args):
    """Prompt the user for the new shelf's name."""
    await _prompt(update, context,
                  "📚 <b>Yeni raf</b>\n\nRafın adını yaz ve gönder "
                  "(örn: <code>Felsefe</code>, <code>Tarih</code>, <code>Polisiye</code>).",
                  {"type": "shelf_new_name"})


async def _cb_covers_grid(update, context, args):
    """Send book covers as a Telegram media group (album) so the user sees
    several covers at once. Telegram caps each album at 10 photos, so we
    chunk into multiple albums when needed.
    """
    from telegram import InputMediaPhoto
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()
    books = [b for b in await data.list_books() if b.cover_url]
    if not books:
        await query.message.reply_text("Henüz kapak görseli olan kitap yok.")
        return
    try:
        async with _Progress(query.message, f"🔄 {len(books)} kapak gönderiliyor…"):
            # Send in chunks of 10 (Telegram album limit)
            for i in range(0, len(books), 10):
                chunk = books[i:i + 10]
                media = []
                for j, b in enumerate(chunk):
                    caption = f"{getattr(b, 'icon', '📖')} {b.short_code} · {b.title[:60]}"
                    if j == 0:
                        media.append(InputMediaPhoto(media=b.cover_url, caption=caption))
                    else:
                        media.append(InputMediaPhoto(media=b.cover_url, caption=caption))
                try:
                    await context.bot.send_media_group(chat_id=chat_id, media=media)
                except Exception as e:
                    logger.warning("bot.covers_grid.chunk_failed", error=str(e))
    except Exception as e:
        logger.error("bot.covers_grid.failed", error=str(e), exc_info=True)
        await query.message.reply_text(
            f"❌ Kapaklar gönderilemedi: <code>{esc(type(e).__name__)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def _cb_book(update, context, args):
    text, kb, cover = await screen_book_detail(int(args[0]))
    await _send_screen(update, context, text, kb, photo_url=cover)


async def _cb_notes(update, context, args):
    offset = int(args[1]) if len(args) > 1 else 0
    text, kb = await screen_notes_for_book(int(args[0]), offset=offset)
    await _send_screen(update, context, text, kb)


async def _cb_note(update, context, args):
    text, kb, file_id = await screen_note_detail(int(args[0]))
    await _send_screen(update, context, text, kb, photo_file_id=file_id)


async def _cb_note_full(update, context, args):
    """Show the full transcript / definition / explanation (Madde 4)."""
    text, kb, file_id = await screen_note_detail(int(args[0]), full=True)
    await _send_screen(update, context, text, kb, photo_file_id=file_id)


async def _cb_note_fav(update, context, args):
    note_id = int(args[0])
    n = await data.get_note(note_id)
    if n:
        await data.update_note(note_id, is_favorite=not n.is_favorite)
    text, kb, file_id = await screen_note_detail(note_id)
    await _send_screen(update, context, text, kb, photo_file_id=file_id)


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


async def _cb_session_edit(update, context, args):
    """Prompt for a new start_page on an active session."""
    sess_id = int(args[0])
    sess = await data.get_session(sess_id)
    if sess is None:
        await update.callback_query.answer("Oturum yok.", show_alert=True)
        return
    cid = _chat_id(update)
    if cid is not None:
        await _set(cid, "awaiting", {"type": "session_edit_page", "session_id": sess_id})
    await _send_screen(
        update, context,
        f"✏️ <b>Oturumu düzenle</b>  ·  <code>{esc(sess.code)}</code>\n\n"
        f"Şu anki başlangıç sayfası: s.{sess.start_page or '?'}.\n"
        "Yeni sayfa numarasını yaz ve gönder (sadece sayı).",
        KB([_nav_row()]),
    )


async def _cb_session_del(update, context, args):
    """Confirm-then-delete an active session (with its notes)."""
    query = update.callback_query
    if not args:
        return
    sub = args[0]
    sess_id = int(args[1]) if len(args) > 1 else None
    if not sess_id:
        return
    sess = await data.get_session(sess_id)
    if not sess:
        await query.answer("Oturum yok.", show_alert=True)
        return
    if sub == "ask":
        book = await data.get_book(sess.book_id)
        title = book.title if book else "?"
        text = (
            f"🗑️ <b>Oturumu sil</b>  ·  <code>{esc(sess.code)}</code>\n\n"
            f"<b>{esc(title)}</b> kitabının oturumu silinecek.\n"
            f"Bu oturuma bağlı <b>{len(sess.notes)}</b> not da silinir.\n\n"
            "Emin misin?"
        )
        kb = [
            [BTN("✅ Evet, sil", _cb("session_del", "yes", sess_id))],
            [BTN("❌ Vazgeç (oturuma dön)", _cb("session_open", sess_id))],
            [BTN("📋 Açık oturumlara dön", "open_sessions")],
            _nav_row(),
        ]
        await _send_screen(update, context, text, KB(kb))
    elif sub == "yes":
        import asyncio as _asyncio
        await _asyncio.to_thread(_delete_session_sync, sess_id)
        await query.answer("🗑️ Oturum silindi")
        cid = _chat_id(update)
        if cid is not None:
            await _clear(cid, "default_session")
        # Prefer to return to the open-sessions list if any remain; else main.
        remaining = await data.list_open_sessions()
        if remaining:
            text, kb = await screen_open_sessions()
        else:
            text, kb = await screen_main()
        await _send_screen(update, context, text, kb)


def _delete_session_sync(sess_id: int) -> None:
    """Synchronous helper for session+notes hard-delete (called via to_thread)."""
    from sqlalchemy import delete as _delete
    with data.db_session() as s:
        s.execute(_delete(data.Note).where(data.Note.session_id == sess_id))
        obj = s.get(data.Session, sess_id)
        if obj:
            s.delete(obj)
    data.mark_dirty()


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
        return
    if args[0] == "more" and len(args) >= 3:
        limit = max(int(args[1]), _LIST_PAGE_SIZE)
        query = args[2]
        text, kb = await screen_search_results(query, limit=limit)
        await _send_screen(update, context, text, kb)


async def _cb_glossary(update, context, args):
    limit = _LIST_PAGE_SIZE
    if args and args[0] == "more" and len(args) >= 2:
        limit = max(int(args[1]), _LIST_PAGE_SIZE)
    text, kb = await screen_glossary(limit=limit)
    await _send_screen(update, context, text, kb)


async def _cb_quotes(update, context, args):
    favorites_only = bool(args) and args[0] == "fav"
    limit = _LIST_PAGE_SIZE
    if len(args) >= 3 and args[1] == "more":
        limit = max(int(args[2]), _LIST_PAGE_SIZE)
    text, kb = await screen_quotes(favorites_only=favorites_only, limit=limit)
    await _send_screen(update, context, text, kb)


async def _cb_notes_hub(update, context, args):
    text, kb = await screen_notes_hub()
    await _send_screen(update, context, text, kb)


async def _cb_notes_cat(update, context, args):
    if not args:
        await update.callback_query.answer("Kategori belirsiz.", show_alert=True)
        return
    category_name = args[0]
    limit = _LIST_PAGE_SIZE
    if len(args) >= 3 and args[1] == "more":
        limit = max(int(args[2]), _LIST_PAGE_SIZE)
    text, kb = await screen_notes_by_category(category_name, limit=limit)
    await _send_screen(update, context, text, kb)


async def _cb_notes_custom(update, context, args):
    """Open a user-defined custom-category note list (Madde 28)."""
    if not args:
        await update.callback_query.answer("Kategori belirsiz.", show_alert=True)
        return
    label = args[0]
    limit = _LIST_PAGE_SIZE
    if len(args) >= 3 and args[1] == "more":
        limit = max(int(args[2]), _LIST_PAGE_SIZE)
    text, kb = await screen_notes_by_custom_label(label, limit=limit)
    await _send_screen(update, context, text, kb)


async def _cb_notes_cat_new(update, context, args):
    """Prompt the user for the name of a new custom category."""
    await _prompt(update, context,
                  "➕ <b>Yeni not kategorisi</b>\n\n"
                  "Kategorinin adını yaz ve gönder. 2-40 karakter, kullanmak istediğin "
                  "kelime ya da kısa ifade.\n\n"
                  "Örn: <code>Refleksiyon</code>, <code>Tartışma</code>, "
                  "<code>Açıklama bekliyor</code>.",
                  {"type": "new_category_name"})


async def _cb_notes_cat_del(update, context, args):
    """Delete a custom category from settings. Notes that used it are NOT
    deleted — they just lose the label (category_label becomes a string that
    no longer matches any button, but the note is still in its enum category).
    """
    if not args:
        return
    label = args[0]
    settings = await data.get_settings()
    cats = list(getattr(settings, "custom_categories", None) or [])
    cats = [c for c in cats if c != label]
    await data.update_settings(custom_categories=cats)
    await update.callback_query.answer(f"🗑️ {label} silindi")
    text, kb = await screen_notes_hub()
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
        # Switching to a built-in category clears any custom label
        draft["category_label"] = None
        draft["definition"] = (
            await ai.define_term(draft["transcript"], cat_enum)
            if cat_enum in (Category.WORD, Category.CONCEPT) else None
        )
        if cid is not None:
            await _set(cid, "draft_note", draft)
        text, kb = await screen_note_confirm(draft)
        await _send_screen(update, context, text, kb)

    elif sub == "ccat":
        # Custom user-defined category (Madde 28)
        label = args[1] if len(args) > 1 else ""
        draft["category_label"] = label or None
        if cid is not None:
            await _set(cid, "draft_note", draft)
        text, kb = await screen_note_confirm(draft)
        await _send_screen(update, context, text, kb)

    elif sub == "full":
        # v1.0.5 (Madde 8) — show the draft's full transcript / definition /
        # explanation as a plain follow-up message. Keeps the editor menu
        # active above; the user can still edit/save afterwards.
        body_parts: list[str] = []
        if draft.get("transcript"):
            body_parts.append(f"<b>Transkript</b>:\n{esc(draft['transcript'])}")
        if draft.get("definition"):
            body_parts.append(f"<b>Tanım</b>:\n{esc(draft['definition'])}")
        if draft.get("explanation"):
            body_parts.append(f"<b>Açıklama</b>:\n{esc(draft['explanation'])}")
        full_text = "\n\n".join(body_parts) or "(taslak boş)"
        await query.message.reply_text(
            full_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )

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
        text, kb = await screen_note_confirm(draft)
        await _send_screen(update, context, text, kb)

    elif sub == "explain":
        book = await data.get_book(draft["book_id"])
        try:
            async with _Progress(query.message, "🔄 Gemini açıklama üretiyor…"):
                draft["explanation"] = await ai.explain_note(draft["transcript"], book)
        except Exception as e:
            await query.answer(f"Açıklama üretilemedi: {type(e).__name__}", show_alert=True)
            return
        if cid is not None:
            await _set(cid, "draft_note", draft)
        text, kb = await screen_note_confirm(draft)
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
                photo_file_id=draft.get("photo_file_id"),
                category_label=draft.get("category_label"),
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
        await query.answer()
        try:
            async with _Progress(query.message, "🔄 JSON arşivi üretiliyor… (tüm kitaplar)"):
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
        await query.answer()
        try:
            async with _Progress(query.message, "🔄 ZIP arşivi üretiliyor… (PDF + JSON + MD, biraz sürebilir)"):
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
        progress_labels = {
            "pdf": "🔄 PDF üretiliyor…",
            "csv": "🔄 CSV oluşturuluyor…",
            "md":  "🔄 Markdown oluşturuluyor…",
        }
        try:
            async with _Progress(query.message, progress_labels[head]):
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
                      "ISBN'i yaz ve gönder.\n\n"
                      "📐 <b>Format</b>\n"
                      "• 13 haneli (<code>978…</code> ya da <code>979…</code>) veya 10 haneli\n"
                      "• Tire (-) ve boşluk olabilir; otomatik temizlenir\n"
                      "• Örn: <code>978-975-08-1234-5</code>  →  <code>9789750812345</code>\n\n"
                      "💡 ISBN bulamıyorsan kapağı çekmeyi dene:\n"
                      "Yeni Kitap → <b>📷 Kapak fotoğrafıyla</b>.",
                      {"type": "newbook_isbn"})

    elif sub == "cover":
        # Tell user to send a photo; mark chat in "newbook_cover" mode.
        # handle_photo dispatches based on this state.
        if cid is not None:
            await _set(cid, "mode", {"name": "newbook_cover"})
        await _send_screen(
            update, context,
            "🏠 Ana › ➕ Yeni Kitap › 📷 <b>Kapak fotoğrafıyla</b>\n\n"
            "Kitabın <b>ön ya da arka kapağının</b> fotoğrafını çek ve gönder.\n\n"
            "Bot Gemini ile resmi okur, içinden:\n"
            "• ISBN (varsa, arka kapakta barkodun yanında)\n"
            "• Başlık\n"
            "• Yazar\n\n"
            "bilgilerini çıkarır. ISBN bulunursa kapak görselini Google Books'tan otomatik alır; "
            "bulunamazsa başlık + yazar ile arama yapar.",
            KB([_nav_row()]),
        )

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
        extra = dict(getattr(book, "extra_fields", None) or {})
        text_lines = [
            f"✏️ <b>{esc(book.title)}</b>  ·  {esc(getattr(book, 'icon', '📖'))}",
            "",
            "Neyi düzelteyim?",
        ]
        if extra:
            text_lines += ["", "<i>Kişisel alanlar:</i>"]
            for k, v in extra.items():
                text_lines.append(f"  • <b>{esc(k)}</b>: {esc(str(v)[:60])}")
        text = "\n".join(text_lines)
        kb = [
            [BTN("📖 Başlık",    _cb("book_edit", book.id, "title")),
             BTN("✍️ Yazar",     _cb("book_edit", book.id, "author"))],
            [BTN("🏷️ Tür",       _cb("book_edit", book.id, "genre")),
             BTN("Alt tür",     _cb("book_edit", book.id, "subgenre"))],
            [BTN("🌐 ISBN",      _cb("book_edit", book.id, "isbn")),
             BTN("🏢 Yayınevi",  _cb("book_edit", book.id, "publisher"))],
            [BTN("📄 Sayfa",     _cb("book_edit", book.id, "total_pages")),
             BTN("📅 Yayın yılı", _cb("book_edit", book.id, "publication_year"))],
            [BTN("🛒 Nereden",   _cb("book_edit", book.id, "bought_from")),
             BTN("💰 Fiyat (TL)", _cb("book_edit", book.id, "price_tl"))],
            [BTN("🏷️ Etiketler", _cb("book_edit", book.id, "tags")),
             BTN("📝 Kişisel not", _cb("book_edit", book.id, "personal_note"))],
            [BTN(f"🎨 İkon ({getattr(book, 'icon', '📖')})", _cb("book_edit", book.id, "icon")),
             BTN(f"🔤 Kod ({book.short_code})", _cb("book_edit", book.id, "short_code"))],
            [BTN("📚 Rafı değiştir", _cb("book_edit", book.id, "shelf"))],
            [BTN("➕ Yeni alan ekle (özel)", _cb("book_edit", book.id, "extra_add"))],
        ]
        if extra:
            kb.append([BTN("🗑️ Özel alan sil", _cb("book_edit", book.id, "extra_del"))])
        kb.append(_nav_row())
        await _send_screen(update, context, text, KB(kb))
        return

    field = sub_args[0]
    # Sub-actions for custom (extra_fields) management
    if field == "extra_add":
        await _prompt(update, context,
                      "➕ <b>Yeni alan</b>\n\nAlan adını yaz ve gönder. "
                      "Sonra değerini soracağım.\n\n"
                      "Örn: <code>Raf kodu</code>, <code>Ödünç verildi</code>, "
                      "<code>İlk okuma tarihi</code>.",
                      {"type": "book_extra_add", "book_id": book_id})
        return
    if field == "extra_del":
        book = await data.get_book(book_id)
        extra = dict(getattr(book, "extra_fields", None) or {})
        kb = []
        for k in extra:
            kb.append([BTN(f"🗑️ {k}", _cb("book_edit", book_id, "extra_del_key", k))])
        kb.append(_nav_row())
        await _send_screen(
            update, context,
            "🗑️ <b>Hangi özel alanı silmek istersin?</b>",
            KB(kb),
        )
        return
    if field == "extra_del_key" and len(sub_args) >= 2:
        key = sub_args[1]
        await data.delete_book_extra_field(book_id, key)
        await update.callback_query.answer(f"🗑️ {key} silindi")
        text, kb, cover = await screen_book_detail(book_id)
        await _send_screen(update, context, text, kb, photo_url=cover)
        return
    if field == "icon":
        await _prompt(update, context,
                      "🎨 <b>İkon seç</b>\n\n"
                      "Bir emoji yaz ve gönder (örn: 📕 📘 📗 📙 🖤 📜 🐉 ⚔️ 🧠 🌹).",
                      {"type": "book_icon", "book_id": book_id})
        return
    if field == "shelf":
        shelves = await data.list_shelves()
        kb = [[BTN("(rafı kaldır)", _cb("book_edit", book_id, "shelf_set", 0))]]
        for sh in shelves:
            kb.append([BTN(f"{sh.icon} {sh.name}", _cb("book_edit", book_id, "shelf_set", sh.id))])
        kb.append([BTN("➕ Yeni raf oluştur", "shelf_new")])
        kb.append(_nav_row())
        await _send_screen(
            update, context,
            "📚 <b>Bu kitabı hangi rafa koyalım?</b>",
            KB(kb),
        )
        return
    if field == "shelf_set" and len(sub_args) >= 2:
        target = int(sub_args[1])
        await data.update_book(book_id, shelf_id=(target if target > 0 else None))
        await update.callback_query.answer("📚 Raf güncellendi")
        text, kb, cover = await screen_book_detail(book_id)
        await _send_screen(update, context, text, kb, photo_url=cover)
        return

    prompts = {
        "title":             "Yeni başlığı yaz.",
        "author":            "Yazar adını yaz (birden fazla ise virgülle ayır).",
        "genre":             "Tür (örn: Felsefe).",
        "subgenre":          "Alt tür (örn: Varoluşçuluk).",
        "isbn":              "ISBN (13 ya da 10 hane).",
        "publisher":         "Yayınevi adı.",
        "publication_year":  "Yayın yılı (sadece sayı, örn 2018).",
        "bought_from":       "Nereden alındı? (örn: D&R, Idefix, hediye).",
        "price_tl":          "Fiyat TL cinsinden (sadece sayı).",
        "tags":              "Etiketleri virgülle ayır ve gönder (örn: <code>felsefe, klasik</code>).",
        "personal_note":     "Kitap hakkındaki kişisel notunu yaz.",
        "total_pages":       "Toplam sayfa sayısını yaz (sadece sayı).",
        "short_code":        "Yeni kısa kod (2-8 büyük harf/rakam).",
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
    await query.answer()
    try:
        async with _Progress(query.message, "🔄 PDF üretiliyor… (kapak + notlar + alıntılar)"):
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


async def _cb_help(update, context, args):
    text, kb = screen_help()
    await _send_screen(update, context, text, kb)


async def _cb_note_share(update, context, args):
    """Step 1/2: pick the page size for the shareable note PDF (Madde 19).

    Tapping a size leads to the font picker (_cb_note_share_font); the actual
    render runs in _cb_note_share_do.
    """
    if not args:
        return
    note_id = int(args[0])
    text = (
        "📤 <b>Notu paylaş — Adım 1/2</b>\n\n"
        "Hangi boyutta görsel istersin?\n"
        "Bir sonraki adımda yazı tipini seçeceksin."
    )
    kb = [
        [BTN("◼️ Kare (1080×1080)",         _cb("note_share_font", note_id, "square"))],
        [BTN("📸 Instagram Post (1080×1080)", _cb("note_share_font", note_id, "post"))],
        [BTN("📱 Instagram Story (1080×1920)", _cb("note_share_font", note_id, "story"))],
        [BTN("📄 A4 (PDF)",                  _cb("note_share_font", note_id, "a4"))],
        [BTN("📃 A5 (PDF)",                  _cb("note_share_font", note_id, "a5"))],
        _nav_row(),
    ]
    await _send_screen(update, context, text, KB(kb))


async def _cb_note_share_font(update, context, args):
    """Step 2/2: pick the font, then trigger the render.

    Callback shape: note_share_font:<note_id>:<fmt>
    """
    if len(args) < 2:
        await update.callback_query.answer("Eksik parametre.", show_alert=True)
        return
    note_id = int(args[0])
    fmt = args[1]
    text = (
        "📤 <b>Notu paylaş — Adım 2/2</b>\n\n"
        "Yazı tipini seç:"
    )
    # Build buttons from data.NOTE_SHARE_FONTS in a stable order
    keys_in_order = ["crimson", "playfair", "cormorant", "ebgaramond", "lora", "merriweather"]
    kb: list[list[InlineKeyboardButton]] = []
    for k in keys_in_order:
        font = data.NOTE_SHARE_FONTS.get(k)
        if not font:
            continue
        kb.append([BTN(font["label"], _cb("note_share_do", note_id, fmt, k))])
    kb.append([BTN("⬅️ Boyutu değiştir", _cb("note_share", note_id))])
    kb.append(_nav_row())
    await _send_screen(update, context, text, KB(kb))


async def _cb_note_share_do(update, context, args):
    """Actually render and send the shareable PDF.

    Callback shape: note_share_do:<note_id>:<fmt>:<font_key>
    `font_key` defaults to 'crimson' for backward compatibility.
    """
    query = update.callback_query
    if len(args) < 2:
        await query.answer("Eksik parametre.", show_alert=True)
        return
    note_id = int(args[0])
    fmt = args[1]
    font_key = args[2] if len(args) > 2 else "crimson"
    note = await data.get_note(note_id)
    if not note:
        await query.answer("Not bulunamadı.", show_alert=True)
        return
    book = await data.get_book(note.book_id)
    user_name = (update.effective_user.full_name if update.effective_user else "")
    await query.answer()
    try:
        font_label = data.NOTE_SHARE_FONTS.get(font_key, {}).get("label", font_key)
        async with _Progress(
            query.message,
            f"🔄 {fmt.upper()} görseli üretiliyor… ({esc(font_label)})",
        ):
            payload, filename, mime = await data.render_note_share(
                note=note, book=book, fmt=fmt, user_name=user_name,
                font_key=font_key,
            )
    except Exception as e:
        logger.error("bot.note_share.failed", note_id=note_id, fmt=fmt,
                     font_key=font_key, error=str(e), exc_info=True)
        await query.message.reply_text(
            f"❌ Paylaşım üretilirken hata: <code>{esc(type(e).__name__)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    # All sizes are returned as PDFs (WeasyPrint output) so we send as document.
    await context.bot.send_document(
        chat_id=query.message.chat_id,
        document=InputFile(io.BytesIO(payload), filename=filename),
        caption=(
            f"📤 <b>{esc(book.title if book else '')}</b> — paylaş "
            f"({fmt.upper()} · {esc(data.NOTE_SHARE_FONTS.get(font_key, {}).get('label', font_key))})\n"
            "PDF'in 1. sayfasının ekran görüntüsünü alıp paylaşabilirsin."
        ),
        parse_mode=ParseMode.HTML,
    )


# ── final dispatch dict ──
_CALLBACKS: dict[str, Callable[..., Awaitable[None]]] = {
    "noop":                  _cb_noop,
    "main":                  _cb_main,
    "back":                  _cb_main,
    "books":                 _cb_books,
    "books_all":             _cb_books_all,
    "book":                  _cb_book,
    "notes":                 _cb_notes,
    "note":                  _cb_note,
    "note_full":             _cb_note_full,
    "note_fav":              _cb_note_fav,
    "note_edit":             _cb_note_edit,
    "note_page":             _cb_note_page,
    "note_del":              _cb_note_del,
    "note_share":            _cb_note_share,
    "note_share_font":       _cb_note_share_font,
    "note_share_do":         _cb_note_share_do,
    "sessions":              _cb_sessions,
    "session_open":          _cb_session_open,
    "session_edit":          _cb_session_edit,
    "session_del":           _cb_session_del,
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
    "notes_hub":             _cb_notes_hub,
    "notes_cat":             _cb_notes_cat,
    "notes_custom":          _cb_notes_custom,
    "notes_cat_new":         _cb_notes_cat_new,
    "notes_cat_del":         _cb_notes_cat_del,
    "shelves":               _cb_shelves,
    "shelf":                 _cb_shelf,
    "shelf_new":             _cb_shelf_new,
    "covers_grid":           _cb_covers_grid,
    "help":                  _cb_help,
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

    # Map slash command → handler (matches `set_bot_commands` list)
    cmd_handlers = [
        ("start",      handle_start_command),
        ("oturum",     handle_command_oturum),
        ("oturumlar",  handle_command_oturumlar),
        ("kitaplar",   handle_command_kitaplar),
        ("yeni",       handle_command_yeni),
        ("ara",        handle_command_ara),
        ("sozluk",     handle_command_sozluk),
        ("alintilar",  handle_command_alintilar),
        ("istatistik", handle_command_istatistik),
        ("ayarlar",    handle_command_ayarlar),
        ("yardim",     handle_command_yardim),
    ]
    if user_filter is not None:
        for name, fn in cmd_handlers:
            application.add_handler(CommandHandler(name, fn, filters=user_filter))
        application.add_handler(MessageHandler(filters.VOICE & user_filter, handle_voice))
        application.add_handler(MessageHandler(filters.PHOTO & user_filter, handle_photo))
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & user_filter, handle_text))
    else:
        for name, fn in cmd_handlers:
            application.add_handler(CommandHandler(name, fn))
        application.add_handler(MessageHandler(filters.VOICE, handle_voice))
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("bot.build_application.success")
    return application


async def set_bot_commands(application: Application) -> None:
    """Register the slash-command menu shown in Telegram's '/' panel."""
    logger.info("bot.set_bot_commands.start")
    await application.bot.set_my_commands([
        BotCommand("start",      "Ana menüye dön"),
        BotCommand("oturum",     "Yeni okuma oturumu başlat"),
        BotCommand("oturumlar",  "Açık oturumları gör"),
        BotCommand("kitaplar",   "Kütüphanemdeki kitaplar"),
        BotCommand("yeni",       "Yeni kitap ekle (yazı / ISBN / kapak)"),
        BotCommand("ara",        "Notlarda ara"),
        BotCommand("sozluk",     "Sözlük (Kelime + Kavram)"),
        BotCommand("alintilar",  "Alıntılar"),
        BotCommand("istatistik", "Okuma istatistiklerim"),
        BotCommand("ayarlar",    "Bot ayarları"),
        BotCommand("yardim",     "Kısa kullanım rehberi"),
    ])
    logger.info("bot.set_bot_commands.success")


# ─────────── Slash command handlers (mirror main menu actions) ───────────

@_safe_handler
async def handle_command_oturum(update, context):
    await handle_callback_proxy(update, context, "start_pick")

@_safe_handler
async def handle_command_oturumlar(update, context):
    await handle_callback_proxy(update, context, "open_sessions")

@_safe_handler
async def handle_command_yardim(update, context):
    await handle_callback_proxy(update, context, "help")

@_safe_handler
async def handle_command_kitaplar(update, context):
    await handle_callback_proxy(update, context, "books")

@_safe_handler
async def handle_command_yeni(update, context):
    await handle_callback_proxy(update, context, "newbook:start")

@_safe_handler
async def handle_command_ara(update, context):
    await handle_callback_proxy(update, context, "search:start")

@_safe_handler
async def handle_command_sozluk(update, context):
    await handle_callback_proxy(update, context, "glossary")

@_safe_handler
async def handle_command_alintilar(update, context):
    await handle_callback_proxy(update, context, "quotes:all")

@_safe_handler
async def handle_command_istatistik(update, context):
    await handle_callback_proxy(update, context, "stats")

@_safe_handler
async def handle_command_ayarlar(update, context):
    await handle_callback_proxy(update, context, "settings")


async def handle_callback_proxy(update, context, callback_data: str) -> None:
    """Route a slash command through the same screen-builder pipeline that
    inline-keyboard callbacks use. Sends a fresh bubble (no edit) because the
    user typed a command, not tapped a button.
    """
    parts = callback_data.split(":")
    head, args = parts[0], parts[1:]
    handler = _CALLBACKS.get(head)
    if handler is None:
        return
    # Wrap update so handler can render as if it were a callback — but we
    # actually want a fresh message, not an edit. Trick: clear callback_query.
    # Simplest path: replicate the relevant screen render manually for the
    # common entry points (main → first menu, etc). For brevity here we just
    # send the right screen using the existing builders.
    if head == "start_pick":
        books = await data.list_books()
        unfinished = [b for b in books if b.status != BookStatus.FINISHED]
        if not unfinished:
            await update.message.reply_text("Devam edebileceğin kitap yok. Önce yeni bir kitap ekle.")
            return
        kb = [[BTN(f"📖 {b.short_code} · {b.title}", _cb("recap", b.id))] for b in unfinished]
        kb.append([BTN("➕ Yeni Kitap Ekle", "newbook:start")])
        kb.append(_nav_row())
        await update.message.reply_text(
            "🏠 Ana › ▶️ <b>Oturum Başlat</b>\n\nHangi kitabı okuyacaksın?",
            reply_markup=KB(kb), parse_mode=ParseMode.HTML,
        )
        return
    if head == "books":
        text, kb = await screen_book_list()
    elif head == "newbook":
        text, kb = screen_newbook_method()
    elif head == "search":
        cid = _chat_id(update)
        if cid is not None:
            await _set(cid, "awaiting", {"type": "search"})
        await update.message.reply_text(
            "🔍 <b>Ara</b>\n\nAramak istediğin kelimeyi yaz ve gönder.",
            reply_markup=KB([_nav_row()]), parse_mode=ParseMode.HTML,
        )
        return
    elif head == "glossary":
        text, kb = await screen_glossary()
    elif head == "quotes":
        text, kb = await screen_quotes(favorites_only=False)
    elif head == "stats":
        text, kb = await screen_stats()
    elif head == "settings":
        text, kb = await screen_settings()
    elif head == "open_sessions":
        text, kb = await screen_open_sessions()
    elif head == "help":
        text, kb = screen_help()
    else:
        text, kb = await screen_main()
    await update.message.reply_text(
        text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )
