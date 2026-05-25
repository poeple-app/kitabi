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

PROMPT_DEFINE_WORD = """Aşağıdaki kelimenin kısa ve anlaşılır bir Türkçe tanımını yaz. 1-2 cümle yeter.
Sadece tanımı döndür, kelimeyi tekrar etme.

Kelime: {text}
"""

PROMPT_DEFINE_CONCEPT = """Aşağıdaki kavramın kısa Türkçe açıklamasını yaz. 2-3 cümle. Hangi alana ait olduğunu belirt, varsa kim ortaya attığını yaz.
Sadece açıklamayı döndür, kavramı tekrar etme.

Kavram: {text}
"""

PROMPT_EXPLAIN = """Aşağıdaki not kullanıcının okuduğu bir kitaptan alınmış. Bu nota 3-5 cümlelik bir açıklama / genişletme ekle.

Bağlam:
- Kitap: {book_title}
- Yazar: {book_author}
- Tür: {book_genre}

Not:
{text}

Açıklama (3-5 cümle):
"""

PROMPT_ANSWER = """Sen kullanıcının kitap okuma asistanısın. Kullanıcı şu kitabı okuyor:

Kitap: {book_title}
Yazar: {book_author}
Tür: {book_genre}

Kullanıcının bu kitap için aldığı notlar (en yenisi son):
{notes_block}

Kullanıcının sorusu:
{question}

Cevabını Türkçe yaz. Eğer notlar arasında konuyla ilgili bir şey varsa onu da hatırlat. 2-5 paragraf olabilir.
"""

PROMPT_AUTO_SUMMARY = """Aşağıdaki notlar bir kitap okuma oturumundan alınmış. Bu oturumu 2-3 cümle ile özetle. Kullanıcının ne okuduğunu, neyi öğrendiğini, ne düşündüğünü yansıt.

Notlar:
{notes_block}

Özet (2-3 cümle):
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


async def ocr_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> tuple[str, int | None]:
    """Extract Turkish text from a book-page photo (OCR via Gemini vision).

    Returns a tuple of (text, page_number). The page number is `None` if the
    model can't detect one in the image. The bot prompts the user when None.

    Raises GeminiCallFailed on total failure.
    """
    t0 = time.time()
    logger.info("ai.ocr_image.start", mime=mime_type, size_bytes=len(image_bytes))
    raw = await _call_gemini(
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            (
                "Bu kitap sayfası fotoğrafındaki metni Türkçe olarak çıkar. "
                "Eğer sayfanın üstünde veya altında bir sayfa numarası görüyorsan "
                "onu da bul.\n\n"
                "Cevabını TAM olarak şu formatta ver, başka hiçbir şey yazma:\n"
                "PAGE: <sayı veya YOK>\n"
                "---\n"
                "<sayfa metni>"
            ),
        ],
        operation="ocr_image",
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
        duration_ms=int((time.time() - t0) * 1000),
    )
    return text, page


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
            contents=[prompt.format(text=text)],
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
            contents=[PROMPT_AUTO_SUMMARY.format(notes_block=notes_block)],
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
