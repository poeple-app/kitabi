"""
AI capabilities for Kitabi, all routed through Google Gemini.

Every function is:
- Async (uses google-genai's async client)
- Logged at entry and exit with timing
- Resilient: transparent retry with exponential backoff AND automatic model
  fallback (gemini-2.5-flash → gemini-2.0-flash → gemini-1.5-flash). If every
  model fails, a `GeminiCallFailed` exception is raised so the bot layer can
  surface a clear message to the user.

Prompts are kept as module-level constants so they can be reviewed and tuned
in one place.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from google import genai
from google.genai import types

from .data import Book, Category, Note

logger = structlog.get_logger(__name__)


_client: genai.Client | None = None


# ─────────────────────────── Model fallback ───────────────────────────


# Ordered list: primary first. If primary fails or is unavailable, we move
# down the list. All listed models support audio, image and text input.
MODEL_FALLBACK: list[str] = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]

# Retry tuning
MAX_RETRIES_PER_MODEL = 2  # retries within a single model before falling back
RETRY_BASE_DELAY_S = 1.5  # exponential backoff base
DEFAULT_TIMEOUT_S = 60.0  # per-call timeout (covers Cloud Run max well)


class GeminiCallFailed(Exception):
    """Raised when every model in the fallback chain has exhausted its retries.

    The original chain of exceptions is preserved via `__cause__` and the
    message includes a human-readable summary so the bot layer can show it
    to the user.
    """


def init_ai(api_key: str) -> None:
    """Initialize the Gemini client. Must be called once at startup."""
    global _client
    logger.info("ai.init.start", primary_model=MODEL_FALLBACK[0], fallbacks=MODEL_FALLBACK[1:])
    _client = genai.Client(api_key=api_key)
    logger.info("ai.init.success")


def _client_or_raise() -> genai.Client:
    if _client is None:
        raise RuntimeError("init_ai() must be called before any AI function")
    return _client


def _is_transient_error(error: Exception) -> bool:
    """Classify whether an error is worth retrying with the same model.

    Transient: rate limits (429), server errors (5xx), timeouts, network blips.
    Permanent (move to next model): auth errors, model-not-found, schema errors.
    """
    msg = str(error).lower()
    status_code: Any = getattr(error, "code", None) or getattr(error, "status_code", None)

    if isinstance(error, asyncio.TimeoutError):
        return True
    if status_code in (408, 429, 500, 502, 503, 504):
        return True
    if any(
        token in msg
        for token in (
            "rate limit",
            "rate_limit",
            "quota",
            "timeout",
            "deadline",
            "unavailable",
            "503",
            "502",
            "500",
            "429",
            "connection",
            "temporarily",
        )
    ):
        return True
    return False


async def _call_gemini(
    contents: list[Any],
    *,
    operation: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Call Gemini with automatic retry + model fallback.

    Args:
        contents: List of parts to send to Gemini (text strings, types.Part, ...)
        operation: Short tag for logging (e.g. "transcribe_voice", "answer_question")
        timeout_s: Per-call timeout

    Returns:
        The model's text response (stripped).

    Raises:
        GeminiCallFailed: If every model in the fallback chain has exhausted retries.
    """
    client = _client_or_raise()
    last_error: Exception | None = None
    t_total = time.time()

    for model_index, model in enumerate(MODEL_FALLBACK):
        for attempt in range(MAX_RETRIES_PER_MODEL):
            t0 = time.time()
            try:
                logger.debug(
                    "ai._call_gemini.attempt",
                    operation=operation,
                    model=model,
                    model_index=model_index,
                    attempt=attempt + 1,
                )
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(model=model, contents=contents),
                    timeout=timeout_s,
                )
                text = (response.text or "").strip()
                # Success — log if we had to fall back to a non-primary model
                if model_index > 0:
                    logger.warning(
                        "ai._call_gemini.fallback_used",
                        operation=operation,
                        model=model,
                        model_index=model_index,
                        total_duration_ms=int((time.time() - t_total) * 1000),
                    )
                else:
                    logger.debug(
                        "ai._call_gemini.success",
                        operation=operation,
                        model=model,
                        duration_ms=int((time.time() - t0) * 1000),
                    )
                return text

            except Exception as e:  # noqa: BLE001 — we re-classify below
                last_error = e
                error_class = type(e).__name__
                duration_ms = int((time.time() - t0) * 1000)

                if _is_transient_error(e) and attempt < MAX_RETRIES_PER_MODEL - 1:
                    # Retry the same model after a backoff
                    delay = RETRY_BASE_DELAY_S * (attempt + 1)
                    logger.warning(
                        "ai._call_gemini.transient_retry",
                        operation=operation,
                        model=model,
                        attempt=attempt + 1,
                        error_class=error_class,
                        error=str(e)[:200],
                        retry_in_s=delay,
                        duration_ms=duration_ms,
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    # Either non-transient or out of retries — move to next model
                    logger.warning(
                        "ai._call_gemini.model_giving_up",
                        operation=operation,
                        model=model,
                        attempt=attempt + 1,
                        error_class=error_class,
                        error=str(e)[:200],
                        duration_ms=duration_ms,
                    )
                    break  # exits the retry loop, continues to next model

    # If we got here, every model in the fallback chain failed.
    err_text = (
        f"Gemini çağrısı '{operation}' tüm modellerde başarısız oldu. "
        f"Son hata: {type(last_error).__name__}: {str(last_error)[:200]}"
    )
    logger.error(
        "ai._call_gemini.exhausted",
        operation=operation,
        tried_models=MODEL_FALLBACK,
        last_error_class=type(last_error).__name__ if last_error else None,
        last_error=str(last_error)[:200] if last_error else None,
        total_duration_ms=int((time.time() - t_total) * 1000),
    )
    raise GeminiCallFailed(err_text) from last_error


# ─────────────────────────── Prompt constants ───────────────────────────


PROMPT_CATEGORIZE = """Sen bir kitap okuma asistanısın. Aşağıdaki Türkçe nota uygun kategoriyi seç.

Kategoriler:
- Alıntı: kitaptan birebir aktarılan cümle veya paragraf
- Fikir: kullanıcının kendi düşüncesi, yorumu, kitap üzerinden tetiklenmiş çağrışım
- Yeni Bilgi: kitabın aktardığı bir bilgi, olgu, tarihsel gerçek
- Kelime: kullanıcının öğrendiği yeni bir kelime
- Kavram: kullanıcının öğrendiği yeni bir kavram, terim, teori
- Özet: bir oturumun sonunda yapılan toplu özet (genelde "bu oturumda...", "bugün...", "şimdiye kadar..." gibi başlar)

Sadece kategori adını yaz, başka hiçbir şey yazma. Örnek cevap: Alıntı

Not:
{text}
"""

# Common style directive — applied to every text-generation prompt to keep
# responses tight (no purple prose, no filler sentences, just signal).
# IMPORTANT: bilgi kaybı yok, sadece kelime sayısı düşürülür.
STYLE_RULES = """\
TARZ KURALLARI (KESİN — bu kurallara uymazsan cevap reddedilir):
1. Maksimum öz. Cevap en geç istenen cümle sayısında bitsin; aksi açıkça belirtilmemişse 2-3 cümleyi geçme.
2. Tüm dolgu kelimeleri yasak:
   "şüphesiz", "kuşkusuz", "bilindiği üzere", "elbette", "açıkça",
   "kesinlikle", "bu açıdan", "bu bağlamda", "değerlendirildiğinde",
   "söylemek gerekir ki", "ifade etmek mümkündür", "denilebilir ki",
   "esas itibarıyla", "bir nevi", "aslında", "tabii ki" — hiçbiri geçmesin.
3. Süslü/lirik dil, edebi mecaz, retorik soru — yok.
4. Soruyu/notu tekrar etme, "evet öyle"/"haklısınız" gibi onay cümleleri ekleme.
5. Madde işareti, başlık, emoji, bold/italic — yok. Düz cümle.
6. Bilgi kaybı YOK. Ne anlatılacaksa anlatılsın — sadece dolduran kelimeler düşsün."""


PROMPT_DEFINE_WORD = """Aşağıdaki kelimenin Türkçe tanımı. KESİNLİKLE 1 cümle, en fazla 20 kelime.
Sadece tanımı döndür, kelimeyi tekrarlama.

{style}

Kelime: {text}
"""

PROMPT_DEFINE_CONCEPT = """Aşağıdaki kavramın Türkçe açıklaması. KESİNLİKLE 2 cümleyi geçme.
1. cümle: tanım. 2. cümle: hangi alana ait + varsa kim ortaya attı.
Sadece açıklamayı döndür, kavramı tekrarlama.

{style}

Kavram: {text}
"""

PROMPT_EXPLAIN = """Aşağıdaki not kullanıcının okuduğu kitaptan alınmış. Bu nota 2-3 cümlelik genişletme ekle.

Bağlam:
- Kitap: {book_title}
- Yazar: {book_author}
- Tür: {book_genre}

{style}

Not:
{text}

Açıklama (en fazla 3 cümle):
"""

PROMPT_ANSWER = """Kullanıcı şu kitabı okuyor:

Kitap: {book_title}
Yazar: {book_author}
Tür: {book_genre}

Kullanıcının bu kitap için aldığı notlar (en yenisi son):
{notes_block}

Kullanıcının sorusu:
{question}

{style}

KESİN LİMİT: cevabın en fazla 3 cümle olsun. Notlardan referans verirken kod kullan (örn "SVC002 notunda…"), uzun alıntı yapma. Eğer 1 cümleyle yeterli cevap verilebiliyorsa, 1 cümleyle ver.

Cevap:
"""

PROMPT_AUTO_SUMMARY = """Aşağıdaki notlar bir okuma oturumundan. Oturumu KESİNLİKLE 2 cümlede özetle:
1. cümle: kullanıcı ne okudu (konu/sayfa aralığı).
2. cümle: en önemli 1-2 öğrenim/düşünce.

{style}

Notlar:
{notes_block}

Özet (2 cümle, fazlası kabul edilmez):
"""

PROMPT_OCR_HIGHLIGHTED = """\
Bu bir kitap sayfası fotoğrafı. Kullanıcı SADECE altını çizdiği, vurguladığı,
fosforladığı ya da kutu içine aldığı parçaları not olarak almak istiyor.

Yapacakların:
1. Sayfadaki altı çizili / fosforlu / kalemle vurgulanmış / kutu içine alınmış
   metinleri bul ve çıkar.
2. Sayfa numarasını üst ya da alt margin'den oku (varsa).
3. EĞER hiçbir vurgu işareti YOKSA, "VURGU_YOK" yaz — tam sayfayı KESİNLİKLE
   kopyalama. Kullanıcının seçmediği metni kaydetmek istemiyoruz.

Cevabını TAM olarak şu formatta ver, başka hiçbir şey yazma:
PAGE: <sayfa numarası veya YOK>
---
<sadece vurgulu kısımlar; birden çok parça varsa her birini ayrı satıra koy>

veya hiç vurgu yoksa:
PAGE: <sayfa numarası veya YOK>
---
VURGU_YOK
"""


PROMPT_PHOTO_QUESTION = """\
Kullanıcı bir kitap sayfasının fotoğrafını gönderdi ve fotoğraf üzerinde
şu soruyu sordu:

SORU: {question}

Görevin:
1. Sayfadaki ilgili metni OCR ile oku (gerekirse tamamını).
2. Soruyu yalnızca sayfadaki bilgiye dayanarak cevapla.
3. Sayfada cevap için yeterli bilgi yoksa açıkça "Sayfada bu bilgi yok" de.

Bağlam (varsa):
- Kitap: {book_title}
- Yazar: {book_author}

{style}

KESİN LİMİT: cevabın en fazla 3 cümle olsun.

Cevap:
"""


PROMPT_EXTRACT_BOOK = """Bu bir kitap kapağı fotoğrafı (ön ya da arka). İçinden şu 3 bilgiyi çıkar:
- ISBN: arka kapakta, 10 veya 13 haneli sayı (barkodun altında veya yanında)
- TITLE: kitabın adı (ön kapakta büyük yazılı)
- AUTHOR: yazar(lar)

Cevabını TAM olarak şu formatta ver, başka hiçbir şey yazma:
ISBN: <13 haneli sayı veya YOK>
TITLE: <başlık veya YOK>
AUTHOR: <yazar adı veya YOK>
"""


PROMPT_AUTHOR_OTHER_BOOKS = """\
Yazarın adı: {author}
Şu anki kitap (bunu LİSTEDE TEKRAR ETME): {current_title}

Görev: Bu yazarın EN ÇOK BİLİNEN/SATAN 3 başka kitabını listele. Sadece kitap adlarını ver.
Format (TAM olarak; başka hiçbir şey ekleme):
1. <Kitap Adı>
2. <Kitap Adı>
3. <Kitap Adı>

Eğer yazarın 3 farklı bilinen kitabı yoksa, var olanları listele. Eğer hiç bulamıyorsan sadece YOK yaz.
"""


# ─────────────────────────── AI functions ───────────────────────────


async def transcribe_voice(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe a Telegram voice message into Turkish text.

    Raises GeminiCallFailed on total failure (caller should surface to user).
    """
    t0 = time.time()
    logger.info("ai.transcribe_voice.start", mime=mime_type, size_bytes=len(audio_bytes))
    text = await _call_gemini(
        contents=[
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            "Bu Türkçe ses kaydını metne çevir. Sadece metni döndür, başka açıklama ekleme.",
        ],
        operation="transcribe_voice",
    )
    logger.info(
        "ai.transcribe_voice.success",
        text_length=len(text),
        duration_ms=int((time.time() - t0) * 1000),
    )
    return text


async def ocr_image(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    *,
    mode: str = "highlight",
) -> tuple[str, int | None]:
    """Extract Turkish text from a book-page photo (OCR via Gemini vision).

    Modes:
        "highlight" (v1.0.2 default): only return underlined / highlighted /
            boxed passages. If the user didn't mark anything, returns the
            sentinel "VURGU_YOK" so the caller can ask for a manual note.
        "full": original behaviour — full-page transcription (used internally
            for question answering and orphan-photo fallback).

    Returns (text, page_number). page_number is None when the model can't read
    one off the image; the bot then prompts the user.

    Raises GeminiCallFailed on total failure.
    """
    t0 = time.time()
    logger.info("ai.ocr_image.start", mime=mime_type, size_bytes=len(image_bytes), mode=mode)
    if mode == "highlight":
        prompt_text = PROMPT_OCR_HIGHLIGHTED
        operation = "ocr_image_highlight"
    else:
        prompt_text = (
            "Bu kitap sayfası fotoğrafındaki metni Türkçe olarak çıkar. "
            "Eğer sayfanın üstünde veya altında bir sayfa numarası görüyorsan "
            "onu da bul.\n\n"
            "Cevabını TAM olarak şu formatta ver, başka hiçbir şey yazma:\n"
            "PAGE: <sayı veya YOK>\n"
            "---\n"
            "<sayfa metni>"
        )
        operation = "ocr_image_full"
    raw = await _call_gemini(
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            prompt_text,
        ],
        operation=operation,
    )
    page: int | None = None
    text = raw
    if raw.upper().startswith("PAGE:"):
        first_line, _, rest = raw.partition("\n")
        page_str = first_line[5:].strip()
        if page_str and page_str.upper() != "YOK":
            try:
                page = int("".join(c for c in page_str if c.isdigit()))
            except ValueError:
                page = None
        # Strip optional `---` divider from the body
        body = rest
        if body.lstrip().startswith("---"):
            body = body.split("---", 1)[-1]
        text = body.strip()
    logger.info(
        "ai.ocr_image.success",
        text_length=len(text),
        page_detected=page,
        mode=mode,
        duration_ms=int((time.time() - t0) * 1000),
    )
    return text, page


async def answer_about_image(
    image_bytes: bytes,
    question: str,
    *,
    book_title: str = "(bilinmiyor)",
    book_author: str = "(bilinmiyor)",
    mime_type: str = "image/jpeg",
) -> str:
    """The user attached a question to the photo's caption: read the page and
    answer their question using only what's on it. Used by `handle_photo`
    when `msg.caption` is non-empty.

    Returns plain text. Raises GeminiCallFailed on total failure.
    """
    t0 = time.time()
    logger.info(
        "ai.answer_about_image.start",
        question_length=len(question), size_bytes=len(image_bytes),
    )
    answer = await _call_gemini(
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            PROMPT_PHOTO_QUESTION.format(
                question=question,
                book_title=book_title,
                book_author=book_author,
                style=STYLE_RULES,
            ),
        ],
        operation="answer_about_image",
    )
    logger.info(
        "ai.answer_about_image.success",
        answer_length=len(answer),
        duration_ms=int((time.time() - t0) * 1000),
    )
    return answer.strip()


async def suggest_category(text: str) -> Category:
    """Classify a note into one of the six categories.

    Falls back to NEW_INFO on parse failure or if every model fails. The bot
    will still save the note — it's better to mis-categorize than to drop.
    """
    t0 = time.time()
    logger.info("ai.suggest_category.start", text_length=len(text))
    try:
        raw = await _call_gemini(
            contents=[PROMPT_CATEGORIZE.format(text=text)],
            operation="suggest_category",
        )
        for cat in Category:
            if cat.value.lower() in raw.lower():
                logger.info(
                    "ai.suggest_category.success",
                    category=cat.value,
                    duration_ms=int((time.time() - t0) * 1000),
                )
                return cat
        logger.warning("ai.suggest_category.parse_fallback", raw=raw[:120])
        return Category.NEW_INFO
    except GeminiCallFailed as e:
        logger.error(
            "ai.suggest_category.gemini_failed",
            error=str(e),
            text_length=len(text),
        )
        return Category.NEW_INFO  # graceful fallback


async def define_term(text: str, kind: Category) -> str | None:
    """Generate a short definition for a Word or Concept.

    Returns None for non-Word/Concept categories, or on AI failure.
    """
    if kind not in (Category.WORD, Category.CONCEPT):
        return None
    t0 = time.time()
    logger.info("ai.define_term.start", kind=kind.value, term_length=len(text))
    try:
        prompt = PROMPT_DEFINE_WORD if kind == Category.WORD else PROMPT_DEFINE_CONCEPT
        definition = await _call_gemini(
            contents=[prompt.format(text=text, style=STYLE_RULES)],
            operation=f"define_{kind.value}",
        )
        logger.info(
            "ai.define_term.success",
            kind=kind.value,
            def_length=len(definition),
            duration_ms=int((time.time() - t0) * 1000),
        )
        return definition or None
    except GeminiCallFailed as e:
        logger.error(
            "ai.define_term.gemini_failed",
            error=str(e),
            kind=kind.value,
        )
        return None  # Definitions are nice-to-have


async def explain_note(text: str, book: Book) -> str | None:
    """Generate a 3-5 sentence expansion/explanation for a note.

    Returns None on failure (caller can surface a friendly message).
    """
    t0 = time.time()
    logger.info("ai.explain_note.start", text_length=len(text), book_id=book.id)
    try:
        explanation = await _call_gemini(
            contents=[
                PROMPT_EXPLAIN.format(
                    text=text,
                    book_title=book.title,
                    book_author=book.author or "bilinmiyor",
                    book_genre=book.genre or "—",
                    style=STYLE_RULES,
                )
            ],
            operation="explain_note",
        )
        logger.info(
            "ai.explain_note.success",
            length=len(explanation),
            book_id=book.id,
            duration_ms=int((time.time() - t0) * 1000),
        )
        return explanation or None
    except GeminiCallFailed as e:
        logger.error(
            "ai.explain_note.gemini_failed",
            error=str(e),
            book_id=book.id,
        )
        return None


async def answer_question(question: str, book: Book, notes: list[Note]) -> str:
    """Answer a question using the book and its accumulated notes as context.

    Raises GeminiCallFailed on total failure (the bot will surface this; Q&A
    can't gracefully degrade — the user wants an answer).
    """
    t0 = time.time()
    logger.info(
        "ai.answer_question.start",
        question_length=len(question),
        book_id=book.id,
        note_count=len(notes),
    )
    # Cap to last 50 notes to avoid token bloat. 50 short notes ≈ <10K tokens.
    notes_block = "\n".join(
        f"- [s.{n.page or '—'} · {n.category.value}] {n.transcript[:200]}"
        for n in notes[-50:]
    ) or "(henüz not yok)"
    answer = await _call_gemini(
        contents=[
            PROMPT_ANSWER.format(
                book_title=book.title,
                book_author=book.author or "bilinmiyor",
                book_genre=book.genre or "—",
                notes_block=notes_block,
                question=question,
                style=STYLE_RULES,
            )
        ],
        operation="answer_question",
    )
    logger.info(
        "ai.answer_question.success",
        answer_length=len(answer),
        book_id=book.id,
        duration_ms=int((time.time() - t0) * 1000),
    )
    return answer


async def auto_summarize_session(notes: list[Note]) -> str | None:
    """Generate a 2-3 sentence auto-summary for a session (if user skipped manual).

    Returns None on empty input or AI failure.
    """
    if not notes:
        return None
    t0 = time.time()
    logger.info("ai.auto_summarize.start", note_count=len(notes))
    try:
        notes_block = "\n".join(
            f"- [s.{n.page or '—'} · {n.category.value}] {n.transcript[:200]}"
            for n in notes
        )
        summary = await _call_gemini(
            contents=[PROMPT_AUTO_SUMMARY.format(notes_block=notes_block, style=STYLE_RULES)],
            operation="auto_summarize",
        )
        logger.info(
            "ai.auto_summarize.success",
            length=len(summary),
            duration_ms=int((time.time() - t0) * 1000),
        )
        return summary or None
    except GeminiCallFailed as e:
        logger.error(
            "ai.auto_summarize.gemini_failed",
            error=str(e),
            note_count=len(notes),
        )
        return None


async def extract_book_from_cover(
    image_bytes: bytes, mime_type: str = "image/jpeg"
) -> dict[str, str | None]:
    """Try to read ISBN / title / author from a book cover photo via Gemini Vision.

    Returns a dict with keys `isbn`, `title`, `author` — each value is either a
    string or None. The caller uses these to look up the rest of the metadata
    via Google Books (ISBN first, then title+author fallback).

    Raises GeminiCallFailed on total AI failure.
    """
    t0 = time.time()
    logger.info("ai.extract_book.start", mime=mime_type, size_bytes=len(image_bytes))
    raw = await _call_gemini(
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            PROMPT_EXTRACT_BOOK,
        ],
        operation="extract_book_from_cover",
    )
    out: dict[str, str | None] = {"isbn": None, "title": None, "author": None}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if value.upper() in ("YOK", "NONE", "N/A", "-", ""):
            value = None
        if key in out:
            out[key] = value
    logger.info(
        "ai.extract_book.success",
        isbn_found=out["isbn"] is not None,
        title_found=out["title"] is not None,
        author_found=out["author"] is not None,
        duration_ms=int((time.time() - t0) * 1000),
    )
    return out


async def list_author_other_books(author: str, current_title: str) -> list[str]:
    """Use Gemini to list 3 well-known other works by the same author.

    Falls back to an empty list on any failure — this is decorative metadata
    for the PDF journal, not critical to the bot's operation.
    """
    if not author:
        return []
    t0 = time.time()
    logger.info("ai.author_other_books.start", author=author, current_title=current_title)
    try:
        raw = await _call_gemini(
            contents=[PROMPT_AUTHOR_OTHER_BOOKS.format(
                author=author, current_title=current_title or "(bilinmiyor)",
            )],
            operation="list_author_other_books",
        )
    except GeminiCallFailed as e:
        logger.warning("ai.author_other_books.failed", error=str(e), author=author)
        return []
    titles: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.upper() == "YOK":
            continue
        # Strip leading "1." / "2)" / "-" markers
        for prefix_n in ("1.", "2.", "3.", "1)", "2)", "3)", "-", "•"):
            if line.startswith(prefix_n):
                line = line[len(prefix_n):].strip()
                break
        if line and line.lower() != current_title.lower():
            titles.append(line[:120])
    titles = titles[:3]
    logger.info(
        "ai.author_other_books.success",
        author=author, count=len(titles),
        duration_ms=int((time.time() - t0) * 1000),
    )
    return titles
