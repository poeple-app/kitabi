"""
Data layer for Kitabi: SQLite schema, ORM models, CRUD queries, search,
settings, ephemeral chat state, and all export formats (PDF, JSON, CSV,
Markdown, ZIP).

The single source of truth is a SQLite file (typically restored from a GCS
bucket on Cloud Run startup). All operations go through SQLAlchemy session
context managers. Synchronous SQLAlchemy is wrapped with `asyncio.to_thread`.

Key design points (v0.1.1):
- Books, Sessions and Notes carry a human-readable `code` (e.g. SUC, SUC-S07,
  SUC001) in addition to the numeric primary key.
- Multiple reading sessions can be open at once. There is no "active book"
  concept anywhere in the data layer.
- A singleton `AppSettings` row stores user preferences (proactive nudges,
  intervals, etc.).
- An `EphemeralState` table persists short-lived chat state across cold-starts.
- WAL journal mode + the SQLite Online Backup API gives a torn-snapshot-safe
  upload to GCS.
- A FTS5 virtual table indexes note text for fast global search.
"""

from __future__ import annotations

import asyncio
import csv
import enum
import html as _html
import io
import json
import re
import sqlite3
import time
import unicodedata
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import httpx
import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Session as OrmSession,
    relationship,
    sessionmaker,
)
from weasyprint import HTML

logger = structlog.get_logger(__name__)

# Display timezone — all user-facing dates are rendered in this zone.
DISPLAY_TZ = ZoneInfo("Europe/Istanbul")


# v1.0.4 — Telegram bot token registered at startup. Used by `render_pdf` to
# download user-attached note photos (file_id) and embed them as base64 data
# URIs in the journal PDF. None disables the feature gracefully — PDF still
# renders, photos just stay as text placeholders.
_telegram_bot_token: str | None = None


def set_telegram_bot_token(token: str | None) -> None:
    """Called once at startup from main.lifespan. Optional — if unset, PDF
    rendering skips photo embedding."""
    global _telegram_bot_token
    _telegram_bot_token = token


async def _fetch_telegram_photo_b64(file_id: str) -> str | None:
    """Download a Telegram photo by file_id and return a base64 data URI.

    Two-step Telegram protocol:
      1. POST /getFile?file_id=… returns the file's relative path on Telegram's CDN
      2. GET https://api.telegram.org/file/bot{TOKEN}/{path} returns the bytes

    Returns None on any failure — caller renders a placeholder instead.
    """
    if not _telegram_bot_token or not file_id:
        return None
    import base64
    api_root = f"https://api.telegram.org/bot{_telegram_bot_token}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{api_root}/getFile", params={"file_id": file_id})
            r.raise_for_status()
            payload = r.json() or {}
            if not payload.get("ok"):
                logger.warning("data.fetch_photo.getfile_not_ok", file_id=file_id[:20])
                return None
            path = payload.get("result", {}).get("file_path")
            if not path:
                return None
            file_url = f"https://api.telegram.org/file/bot{_telegram_bot_token}/{path}"
            r2 = await client.get(file_url)
            r2.raise_for_status()
            blob = r2.content
        # Telegram photos are JPEG by default
        mime = "image/jpeg"
        if path.lower().endswith(".png"):
            mime = "image/png"
        b64 = base64.b64encode(blob).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        logger.warning("data.fetch_photo.failed", file_id=file_id[:20], error=str(e))
        return None


def utcnow() -> datetime:
    """Timezone-aware UTC `datetime` (replacement for the deprecated utcnow())."""
    return datetime.now(timezone.utc)


def to_local(dt: datetime | None) -> datetime | None:
    """Convert a stored (naive-UTC or aware) datetime to the display tz."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ)


# ─────────────────────────── Enums ───────────────────────────


class Category(str, enum.Enum):
    """Note categories. The string value matches the Turkish user-facing label."""

    QUOTE = "Alıntı"
    IDEA = "Fikir"
    NEW_INFO = "Yeni Bilgi"
    WORD = "Kelime"
    CONCEPT = "Kavram"
    SUMMARY = "Özet"


class BookStatus(str, enum.Enum):
    """Reading status of a book."""

    NOT_STARTED = "Başlamadı"
    READING = "Okuyor"
    PAUSED = "Duraklatıldı"
    FINISHED = "Bitti"


# ─────────────────────────── ORM models ───────────────────────────


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all Kitabi models."""


class Shelf(Base):
    """A bookshelf — a user-defined grouping of books.

    Used when the library grows beyond ~10 books. Each book optionally links
    to one shelf via Book.shelf_id; books without a shelf appear under
    "Raflandırılmamış".
    """

    __tablename__ = "shelves"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    icon = Column(String(8), default="📚", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Book(Base):
    """A book the user is reading or has read."""

    __tablename__ = "books"

    id = Column(Integer, primary_key=True)
    short_code = Column(String(8), unique=True, index=True, nullable=False)
    title = Column(String(255), nullable=False, index=True)
    author = Column(String(255))
    isbn = Column(String(32), index=True)
    publisher = Column(String(255))  # v1.0.2: filled from Google Books / Open Library
    publication_year = Column(Integer)  # v1.0.2
    genre = Column(String(100))
    subgenre = Column(String(100))
    total_pages = Column(Integer)
    read_pages = Column(Integer, default=0, nullable=False)
    status = Column(SAEnum(BookStatus), default=BookStatus.NOT_STARTED, nullable=False)
    cover_url = Column(String(500))
    icon = Column(String(8), default="📖", nullable=False)  # v1.0.2: per-book emoji
    bought_from = Column(String(255))
    price_tl = Column(Integer)
    bought_at = Column(DateTime)
    tags = Column(JSON, default=list)
    personal_note = Column(Text)
    goodreads_url = Column(String(500))
    # v1.0.2: list of other works by the same author (filled at lookup time)
    author_other_books = Column(JSON, default=list)
    # v1.0.2: user-defined ad-hoc fields, e.g. {"raf_kodu": "A3", "ödünç": "Mert"}
    extra_fields = Column(JSON, default=dict)
    shelf_id = Column(
        Integer, ForeignKey("shelves.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # Note/session sequence per book (drives Note.code / Session.code).
    next_note_number = Column(Integer, default=1, nullable=False)
    next_session_number = Column(Integer, default=1, nullable=False)
    # Finish-ritual fields (filled when status moves to FINISHED).
    rating = Column(Integer)  # 1..5
    one_line_review = Column(Text)
    would_recommend = Column(Boolean)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    sessions = relationship(
        "Session",
        back_populates="book",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    notes = relationship(
        "Note",
        back_populates="book",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    shelf = relationship("Shelf", lazy="joined")


class Session(Base):
    """A single reading session for a book."""

    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    code = Column(String(16), unique=True, index=True, nullable=False)
    book_id = Column(
        Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at = Column(DateTime, default=utcnow, nullable=False)
    ended_at = Column(DateTime)
    start_page = Column(Integer)
    end_page = Column(Integer)
    duration_min = Column(Integer)

    book = relationship("Book", back_populates="sessions")
    notes = relationship("Note", back_populates="session", lazy="selectin")


class Note(Base):
    """A single note taken while reading."""

    __tablename__ = "notes"

    id = Column(Integer, primary_key=True)
    code = Column(String(16), unique=True, index=True, nullable=False)
    book_id = Column(
        Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id = Column(
        Integer, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category = Column(SAEnum(Category), nullable=False, index=True)
    page = Column(Integer)
    transcript = Column(Text, nullable=False)
    definition = Column(Text)
    explanation = Column(Text)
    is_favorite = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    from_qa = Column(Boolean, default=False, nullable=False)
    # v1.0.2: attached cover/scene photo (Telegram file_id; rendered in PDF if downloadable)
    photo_file_id = Column(String(255))
    # v1.0.2: photo that didn't OCR to text — kept as memory/scene rather than a transcript-bearing page
    is_orphan_photo = Column(Boolean, default=False, nullable=False)
    # v1.0.4: user-defined category label (overrides Category.value in UI when set).
    # We keep Note.category as the legacy enum (defaults to NEW_INFO for custom-only
    # notes); category_label is consulted first when present.
    category_label = Column(String(80))

    book = relationship("Book", back_populates="notes")
    session = relationship("Session", back_populates="notes")


class AppSettings(Base):
    """Singleton user-preference row (id is hard-coded to 1)."""

    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)  # always 1
    nudge_enabled = Column(Boolean, default=True, nullable=False)
    nudge_interval_days = Column(Integer, default=3, nullable=False)
    auto_explain = Column(Boolean, default=False, nullable=False)
    auto_categorize = Column(Boolean, default=True, nullable=False)
    summary_prompt_on_end = Column(Boolean, default=True, nullable=False)
    # v1.0.4: list of user-defined extra category labels (e.g. ["Refleksiyon", "Tartışma"]).
    # These appear alongside the 6 built-in Category enum entries in note pickers.
    custom_categories = Column(JSON, default=list)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class EphemeralState(Base):
    """Short-lived chat state persisted across container cold starts.

    Used for `awaiting`, `draft_note`, `mode`, `question_book_id`, etc.
    Values are JSON-serialised. Expired rows are deleted on read.
    """

    __tablename__ = "ephemeral_state"

    chat_id = Column(Integer, primary_key=True)
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


# ─────────────────────────── Engine + session management ───────────────────────────


_engine = None
_session_factory = None
_db_path: str | None = None


def _enable_sqlite_extras(dbapi_conn, _conn_record) -> None:
    """Per-connection PRAGMAs: WAL, foreign keys, busy_timeout."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def init_db(db_path: str) -> None:
    """Initialize the SQLite engine and create tables. Idempotent."""
    global _engine, _session_factory, _db_path
    logger.info("data.init_db.start", db_path=db_path)
    _db_path = db_path
    _engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    event.listen(_engine, "connect", _enable_sqlite_extras)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)
    _migrate_add_missing_columns()
    _setup_fts5()
    _ensure_singleton_settings()
    logger.info("data.init_db.success", db_path=db_path)


def _migrate_add_missing_columns() -> None:
    """Light-weight schema migration: ADD any column that the ORM model has but
    the on-disk table is missing.

    `create_all` only creates NEW tables; it never alters existing ones. We need
    this for forward-compatible deploys where a user's DB was created on v1.0.1
    and we add new columns in v1.0.2 (icon, extra_fields, shelf_id, …).
    """
    if _engine is None:
        return
    # Map: table → list of (column_name, SQL type with default)
    add_cols: dict[str, list[tuple[str, str]]] = {
        "books": [
            ("publisher",          "VARCHAR(255)"),
            ("publication_year",   "INTEGER"),
            ("icon",               "VARCHAR(8) NOT NULL DEFAULT '📖'"),
            ("author_other_books", "TEXT DEFAULT '[]'"),
            ("extra_fields",       "TEXT DEFAULT '{}'"),
            ("shelf_id",           "INTEGER"),
        ],
        "notes": [
            ("photo_file_id",     "VARCHAR(255)"),
            ("is_orphan_photo",   "BOOLEAN NOT NULL DEFAULT 0"),
            ("category_label",    "VARCHAR(80)"),
        ],
        "app_settings": [
            ("custom_categories", "TEXT DEFAULT '[]'"),
        ],
    }
    try:
        with _engine.begin() as conn:
            for table, cols in add_cols.items():
                existing = {row[1] for row in conn.exec_driver_sql(
                    f"PRAGMA table_info({table})"
                ).fetchall()}
                for col_name, col_decl in cols:
                    if col_name in existing:
                        continue
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {col_name} {col_decl}"
                    )
                    logger.info("data.migration.added_column", table=table, column=col_name)
    except Exception as e:
        logger.error("data.migration.failed", error=str(e), exc_info=True)


def _setup_fts5() -> None:
    """Create the FTS5 virtual table + triggers + initial rebuild."""
    if _engine is None:
        return
    statements = [
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
            transcript, definition, explanation,
            content='notes', content_rowid='id'
        )
        """,
        """
        CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
            INSERT INTO notes_fts(rowid, transcript, definition, explanation)
            VALUES (new.id, new.transcript, COALESCE(new.definition,''),
                    COALESCE(new.explanation,''));
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
            INSERT INTO notes_fts(notes_fts, rowid, transcript, definition, explanation)
            VALUES ('delete', old.id, old.transcript, COALESCE(old.definition,''),
                    COALESCE(old.explanation,''));
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
            INSERT INTO notes_fts(notes_fts, rowid, transcript, definition, explanation)
            VALUES ('delete', old.id, old.transcript, COALESCE(old.definition,''),
                    COALESCE(old.explanation,''));
            INSERT INTO notes_fts(rowid, transcript, definition, explanation)
            VALUES (new.id, new.transcript, COALESCE(new.definition,''),
                    COALESCE(new.explanation,''));
        END
        """,
    ]
    try:
        with _engine.begin() as conn:
            for stmt in statements:
                conn.exec_driver_sql(stmt)
        logger.info("data.fts5.ready")
    except Exception as e:
        logger.warning("data.fts5.setup_failed", error=str(e))


def _ensure_singleton_settings() -> None:
    """Create the singleton AppSettings row if missing."""
    with db_session() as s:
        existing = s.get(AppSettings, 1)
        if existing is None:
            s.add(AppSettings(id=1))


@contextmanager
def db_session() -> Iterator[OrmSession]:
    """Yield a SQLAlchemy session. Commits on success, rolls back on exception."""
    if _session_factory is None:
        raise RuntimeError("init_db() must be called before db_session()")
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ─────────────────────────── GCS persistence (consistent backup) ───────────────────────────
#
# Cloud Run's local filesystem is ephemeral, so we periodically snapshot the
# SQLite file to a Cloud Storage bucket. On container startup we download the
# latest snapshot before opening the DB. On shutdown (and on a periodic timer)
# we upload — using the SQLite online backup API so the snapshot is consistent
# even if writes are happening at that exact moment.

_storage_client: Any = None
_gcs_bucket: Any = None
_gcs_db_path: str | None = None
_gcs_blob_name = "kitabi.db"
_db_dirty = True  # uploaded blob may differ from on-disk DB

_local_engine = None  # not used; only here so older code doesn't reference it


def mark_dirty() -> None:
    """Mark the DB as having unsynced writes (signals to periodic sync)."""
    global _db_dirty
    _db_dirty = True


def init_gcs_backup(bucket_name: str, db_path: str) -> None:
    """Initialize GCS-backed SQLite persistence.

    If `bucket_name` is empty, GCS backup is disabled and the DB stays purely
    on the container's local disk.

    If `bucket_name` IS set but the download fails, we re-raise — the bot must
    NOT start with an empty DB and then overwrite the good snapshot. The caller
    (main.py lifespan) lets this exception propagate, Cloud Run restarts.
    """
    global _storage_client, _gcs_bucket, _gcs_db_path
    if not bucket_name:
        logger.info(
            "data.gcs.disabled",
            reason="GCS_BUCKET_NAME not set; SQLite will be ephemeral",
        )
        return
    _gcs_db_path = db_path
    # Lazy import so the package isn't required when GCS is disabled.
    from google.cloud import storage  # type: ignore

    _storage_client = storage.Client()
    _gcs_bucket = _storage_client.bucket(bucket_name)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    blob = _gcs_bucket.blob(_gcs_blob_name)
    if blob.exists():
        blob.download_to_filename(db_path)
        size = Path(db_path).stat().st_size
        logger.info(
            "data.gcs.downloaded",
            bucket=bucket_name,
            size_bytes=size,
            db_path=db_path,
        )
    else:
        logger.info(
            "data.gcs.no_existing_snapshot",
            bucket=bucket_name,
            db_path=db_path,
        )


def _consistent_snapshot(target_path: str) -> int:
    """Use SQLite Online Backup API to copy the live DB to `target_path`.

    This works even while writes are in flight — the source DB's WAL is
    correctly merged into the destination. Returns destination file size.
    """
    if _db_path is None:
        raise RuntimeError("init_db() must run before snapshotting")
    # Use a fresh sqlite3 connection (the backup API is on the connection,
    # not on SQLAlchemy). This is fine because WAL allows multiple readers.
    src = sqlite3.connect(_db_path)
    dst = sqlite3.connect(target_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return Path(target_path).stat().st_size


async def sync_to_gcs(force: bool = False) -> bool:
    """Best-effort upload of a consistent SQLite snapshot to GCS.

    Skips the upload entirely if no writes have happened since the last sync
    (unless `force=True`, used at shutdown).
    """
    global _db_dirty
    if _gcs_bucket is None or _gcs_db_path is None:
        return False
    if not _db_dirty and not force:
        logger.debug("data.gcs.sync_skipped_clean")
        return True

    def _impl() -> int:
        tmp_path = f"{_gcs_db_path}.snapshot"
        try:
            size = _consistent_snapshot(tmp_path)
            blob = _gcs_bucket.blob(_gcs_blob_name)
            blob.upload_from_filename(tmp_path)
            return size
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    t0 = time.time()
    try:
        size = await asyncio.to_thread(_impl)
        _db_dirty = False
        logger.debug(
            "data.gcs.uploaded",
            size_bytes=size,
            duration_ms=int((time.time() - t0) * 1000),
            forced=force,
        )
        return True
    except Exception as e:
        logger.warning(
            "data.gcs.upload_failed",
            error=str(e),
            duration_ms=int((time.time() - t0) * 1000),
            exc_info=True,
        )
        return False


async def periodic_sync_loop(interval_s: float = 60.0) -> None:
    """Background task: snapshot SQLite to GCS every `interval_s` seconds."""
    logger.info("data.gcs.periodic_sync.start", interval_s=interval_s)
    try:
        while True:
            await asyncio.sleep(interval_s)
            await sync_to_gcs()
    except asyncio.CancelledError:
        logger.info("data.gcs.periodic_sync.cancelled")
        raise


# ─────────────────────────── short-code / note-code helpers ───────────────────────────


_ALPHA_FOLD = {
    "ç": "c", "Ç": "C", "ğ": "g", "Ğ": "G", "ı": "i", "İ": "I",
    "ö": "o", "Ö": "O", "ş": "s", "Ş": "S", "ü": "u", "Ü": "U",
}


def _ascii_upper(s: str) -> str:
    """Fold Turkish characters and uppercase. Strips remaining accents."""
    s = "".join(_ALPHA_FOLD.get(c, c) for c in s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.upper()


SHORT_CODE_RE = re.compile(r"^[A-Z0-9]{2,8}$")


def normalize_short_code(raw: str) -> str | None:
    """Validate and normalise a user-supplied short code (e.g. 'suc' -> 'SUC')."""
    if not raw:
        return None
    candidate = _ascii_upper(raw).strip()
    candidate = re.sub(r"[^A-Z0-9]", "", candidate)
    if SHORT_CODE_RE.match(candidate):
        return candidate
    return None


def _auto_short_code(title: str) -> str:
    """Generate a 3-char code from a title's first letters."""
    upper = _ascii_upper(title)
    # Take first letter of each word, max 3.
    parts = [w for w in re.split(r"[^A-Z0-9]+", upper) if w]
    if not parts:
        return "KIT"
    if len(parts) >= 3:
        return (parts[0][:1] + parts[1][:1] + parts[2][:1]) or "KIT"
    if len(parts) == 2:
        return (parts[0][:2] + parts[1][:1]) or "KIT"
    return parts[0][:3] or "KIT"


def _unique_short_code(s: OrmSession, desired: str) -> str:
    """Return `desired` if free; otherwise append a numeric suffix."""
    base = desired
    candidate = base
    n = 2
    while s.scalar(select(func.count(Book.id)).where(Book.short_code == candidate)) > 0:
        suffix = str(n)
        # Keep total <= 8 chars
        candidate = (base[: 8 - len(suffix)] + suffix)
        n += 1
        if n > 999:  # safety
            raise RuntimeError("Could not allocate unique short_code")
    return candidate


# ─────────────────────────── Book CRUD ───────────────────────────


async def get_book(book_id: int) -> Book | None:
    """Fetch a book by its primary key."""
    def _impl() -> Book | None:
        with db_session() as s:
            return s.get(Book, book_id)
    return await asyncio.to_thread(_impl)


async def get_book_by_code(code: str) -> Book | None:
    """Fetch a book by its short_code (case-insensitive)."""
    norm = normalize_short_code(code)
    if not norm:
        return None

    def _impl() -> Book | None:
        with db_session() as s:
            return s.scalars(select(Book).where(Book.short_code == norm)).first()
    return await asyncio.to_thread(_impl)


async def list_books(status: BookStatus | None = None) -> list[Book]:
    """List all books, optionally filtered by status."""
    def _impl() -> list[Book]:
        with db_session() as s:
            q = select(Book).order_by(Book.created_at.desc())
            if status:
                q = q.where(Book.status == status)
            return list(s.scalars(q).all())
    return await asyncio.to_thread(_impl)


async def create_book(
    *,
    title: str,
    short_code: str | None = None,
    **fields: Any,
) -> Book:
    """Insert a new book; allocates a short_code if not provided.

    `short_code` can be user-supplied (2-8 alnum chars, normalised); falls
    back to auto-derivation from the title if missing or invalid.
    """
    logger.info("data.create_book.start", title=title, requested_code=short_code)

    def _impl() -> Book:
        with db_session() as s:
            desired = normalize_short_code(short_code) if short_code else None
            if not desired:
                desired = _auto_short_code(title)
            code = _unique_short_code(s, desired)
            book = Book(title=title, short_code=code, **fields)
            s.add(book)
            s.flush()
            s.refresh(book)
            return book

    result = await asyncio.to_thread(_impl)
    mark_dirty()
    logger.info(
        "data.create_book.success",
        book_id=result.id,
        title=result.title,
        short_code=result.short_code,
    )
    return result


async def update_book(book_id: int, **fields: Any) -> Book | None:
    """Patch fields on an existing book. `short_code` is re-validated."""
    logger.info("data.update_book.start", book_id=book_id, fields=list(fields.keys()))

    def _impl() -> Book | None:
        with db_session() as s:
            book = s.get(Book, book_id)
            if book is None:
                return None
            if "short_code" in fields and fields["short_code"]:
                norm = normalize_short_code(fields["short_code"])
                if norm and norm != book.short_code:
                    fields["short_code"] = _unique_short_code(s, norm)
                else:
                    fields.pop("short_code")
            for k, v in fields.items():
                setattr(book, k, v)
            s.flush()
            s.refresh(book)
            return book

    result = await asyncio.to_thread(_impl)
    mark_dirty()
    return result


async def delete_book(book_id: int) -> bool:
    """Delete a book and all its sessions/notes (cascade)."""
    logger.info("data.delete_book.start", book_id=book_id)

    def _impl() -> bool:
        with db_session() as s:
            book = s.get(Book, book_id)
            if book is None:
                return False
            s.delete(book)
            return True

    ok = await asyncio.to_thread(_impl)
    mark_dirty()
    logger.info("data.delete_book.done", book_id=book_id, success=ok)
    return ok


# ─────────────────────────── Session CRUD ───────────────────────────


async def active_session_for_book(book_id: int) -> Session | None:
    """Return the unfinished reading session for a book, if any."""
    def _impl() -> Session | None:
        with db_session() as s:
            q = (
                select(Session)
                .where(Session.book_id == book_id, Session.ended_at.is_(None))
                .order_by(Session.started_at.desc())
            )
            return s.scalars(q).first()
    return await asyncio.to_thread(_impl)


async def list_open_sessions() -> list[Session]:
    """All currently open (unfinished) sessions, newest first."""
    def _impl() -> list[Session]:
        with db_session() as s:
            q = (
                select(Session)
                .where(Session.ended_at.is_(None))
                .order_by(Session.started_at.desc())
            )
            return list(s.scalars(q).all())
    return await asyncio.to_thread(_impl)


async def list_sessions_for_book(book_id: int) -> list[Session]:
    """All sessions for a book, newest first."""
    def _impl() -> list[Session]:
        with db_session() as s:
            q = (
                select(Session)
                .where(Session.book_id == book_id)
                .order_by(Session.started_at.desc())
            )
            return list(s.scalars(q).all())
    return await asyncio.to_thread(_impl)


async def get_session(session_id: int) -> Session | None:
    """Fetch a session by its primary key."""
    def _impl() -> Session | None:
        with db_session() as s:
            return s.get(Session, session_id)
    return await asyncio.to_thread(_impl)


async def start_session(book_id: int, start_page: int) -> Session:
    """Open a new reading session and mark the book as 'Reading'."""
    logger.info("data.start_session.start", book_id=book_id, page=start_page)

    def _impl() -> Session:
        with db_session() as s:
            book = s.get(Book, book_id)
            if book is None:
                raise ValueError(f"Book {book_id} not found")
            session_code = f"{book.short_code}-S{book.next_session_number:02d}"
            book.next_session_number += 1
            if book.status in (BookStatus.NOT_STARTED, BookStatus.PAUSED):
                book.status = BookStatus.READING
            session = Session(
                code=session_code,
                book_id=book_id,
                start_page=start_page,
                started_at=utcnow(),
            )
            s.add(session)
            s.flush()
            s.refresh(session)
            return session

    result = await asyncio.to_thread(_impl)
    mark_dirty()
    logger.info(
        "data.start_session.success",
        session_id=result.id,
        code=result.code,
        book_id=book_id,
    )
    return result


async def end_session(session_id: int, end_page: int) -> Session | None:
    """Close a reading session; compute duration, update book progress."""
    logger.info("data.end_session.start", session_id=session_id, end_page=end_page)

    def _impl() -> Session | None:
        with db_session() as s:
            session = s.get(Session, session_id)
            if session is None:
                return None
            session.ended_at = utcnow()
            session.end_page = end_page
            if session.started_at:
                started = session.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                delta_min = (session.ended_at - started).total_seconds() / 60
                session.duration_min = max(1, round(delta_min))
            book = s.get(Book, session.book_id)
            if book:
                book.read_pages = max(book.read_pages or 0, end_page)
                if book.total_pages and book.read_pages >= book.total_pages:
                    book.status = BookStatus.FINISHED
            s.flush()
            s.refresh(session)
            return session

    result = await asyncio.to_thread(_impl)
    mark_dirty()
    return result


async def pause_session(session_id: int) -> Session | None:
    """Close a session without finishing the book; book moves to PAUSED."""
    def _impl() -> Session | None:
        with db_session() as s:
            session = s.get(Session, session_id)
            if session is None:
                return None
            session.ended_at = utcnow()
            session.end_page = session.start_page  # unknown, keep last known
            if session.started_at:
                started = session.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                delta_min = (session.ended_at - started).total_seconds() / 60
                session.duration_min = max(1, round(delta_min))
            book = s.get(Book, session.book_id)
            if book and book.status == BookStatus.READING:
                book.status = BookStatus.PAUSED
            s.flush()
            s.refresh(session)
            return session

    result = await asyncio.to_thread(_impl)
    mark_dirty()
    return result


# ─────────────────────────── Note CRUD ───────────────────────────


async def add_note(
    *,
    book_id: int,
    session_id: int | None,
    category: Category,
    page: int | None,
    transcript: str,
    definition: str | None = None,
    explanation: str | None = None,
    from_qa: bool = False,
    photo_file_id: str | None = None,
    is_orphan_photo: bool = False,
    category_label: str | None = None,
) -> Note:
    """Insert a new note. Allocates `code` like 'SUC001' from the book counter."""
    logger.info(
        "data.add_note.start",
        book_id=book_id,
        session_id=session_id,
        category=category.value,
        page=page,
        from_qa=from_qa,
        is_orphan_photo=is_orphan_photo,
    )

    def _impl() -> Note:
        with db_session() as s:
            book = s.get(Book, book_id)
            if book is None:
                raise ValueError(f"Book {book_id} not found")
            note_code = f"{book.short_code}{book.next_note_number:03d}"
            book.next_note_number += 1
            note = Note(
                code=note_code,
                book_id=book_id,
                session_id=session_id,
                category=category,
                page=page,
                transcript=transcript,
                definition=definition,
                explanation=explanation,
                from_qa=from_qa,
                photo_file_id=photo_file_id,
                is_orphan_photo=is_orphan_photo,
                category_label=category_label,
            )
            s.add(note)
            s.flush()
            s.refresh(note)
            return note

    result = await asyncio.to_thread(_impl)
    mark_dirty()
    logger.info("data.add_note.success", note_id=result.id, code=result.code)
    return result


async def get_note(note_id: int) -> Note | None:
    """Fetch a note by primary key."""
    def _impl() -> Note | None:
        with db_session() as s:
            return s.get(Note, note_id)
    return await asyncio.to_thread(_impl)


async def update_note(note_id: int, **fields: Any) -> Note | None:
    """Patch fields on a note (transcript, page, category, is_favorite, etc.)."""
    logger.info("data.update_note.start", note_id=note_id, fields=list(fields.keys()))

    def _impl() -> Note | None:
        with db_session() as s:
            note = s.get(Note, note_id)
            if note is None:
                return None
            for k, v in fields.items():
                if k == "code":  # never patch the code
                    continue
                setattr(note, k, v)
            s.flush()
            s.refresh(note)
            return note

    result = await asyncio.to_thread(_impl)
    mark_dirty()
    return result


async def delete_note(note_id: int) -> bool:
    """Delete a single note."""
    def _impl() -> bool:
        with db_session() as s:
            note = s.get(Note, note_id)
            if note is None:
                return False
            s.delete(note)
            return True

    ok = await asyncio.to_thread(_impl)
    mark_dirty()
    return ok


async def notes_for_book(
    book_id: int,
    category: Category | None = None,
    offset: int = 0,
    limit: int = 25,
) -> tuple[list[Note], int]:
    """Notes for a book + total count. Paginated."""
    def _impl() -> tuple[list[Note], int]:
        with db_session() as s:
            count_q = select(func.count(Note.id)).where(Note.book_id == book_id)
            q = select(Note).where(Note.book_id == book_id).order_by(Note.created_at.asc())
            if category:
                count_q = count_q.where(Note.category == category)
                q = q.where(Note.category == category)
            total = s.scalar(count_q) or 0
            q = q.offset(offset).limit(limit)
            return list(s.scalars(q).all()), total
    return await asyncio.to_thread(_impl)


async def notes_for_session(session_id: int) -> list[Note]:
    """Return all notes recorded in a specific session."""
    def _impl() -> list[Note]:
        with db_session() as s:
            q = (
                select(Note)
                .where(Note.session_id == session_id)
                .order_by(Note.created_at.asc())
            )
            return list(s.scalars(q).all())
    return await asyncio.to_thread(_impl)


async def summaries_for_book(book_id: int) -> list[Note]:
    """Summary-category notes — used to recap on the next session start."""
    def _impl() -> list[Note]:
        with db_session() as s:
            q = (
                select(Note)
                .where(Note.book_id == book_id, Note.category == Category.SUMMARY)
                .order_by(Note.created_at.asc())
            )
            return list(s.scalars(q).all())
    return await asyncio.to_thread(_impl)


# ─────────────────────────── Search / glossary / quotes ───────────────────────────


async def search_notes(query: str, limit: int = 30) -> list[tuple[Note, Book]]:
    """Full-text search across all notes (FTS5).

    Falls back to LIKE if FTS5 isn't available.
    """
    if not query.strip() or _engine is None:
        return []
    safe_query = query.replace('"', "").strip()

    def _impl() -> list[tuple[Note, Book]]:
        from sqlalchemy import text as _sa_text

        with db_session() as s:
            try:
                # FTS5 MATCH path: prefix-token query with bm25 ranking.
                rows = list(
                    s.execute(
                        _sa_text(
                            "SELECT n.id FROM notes_fts f "
                            "JOIN notes n ON n.id = f.rowid "
                            "WHERE notes_fts MATCH :q "
                            "ORDER BY bm25(notes_fts) LIMIT :lim"
                        ),
                        {"q": safe_query + "*", "lim": limit},
                    )
                )
                note_ids = [r[0] for r in rows]
            except Exception as e:
                logger.warning("data.search.fts_failed_fallback_like", error=str(e))
                like = f"%{safe_query}%"
                rows = list(
                    s.execute(
                        select(Note.id)
                        .where(Note.transcript.ilike(like))
                        .order_by(Note.created_at.desc())
                        .limit(limit)
                    )
                )
                note_ids = [r[0] for r in rows]
            if not note_ids:
                return []
            results: list[tuple[Note, Book]] = []
            for nid in note_ids:
                note = s.get(Note, nid)
                if note is None:
                    continue
                book = s.get(Book, note.book_id)
                if book:
                    results.append((note, book))
            return results

    return await asyncio.to_thread(_impl)


async def list_glossary() -> list[tuple[Note, Book]]:
    """All WORD-category notes (sözlük), alphabetical by transcript.

    v1.0.6: Previously included CONCEPT too. Now strict — sözlük sadece
    "Kelime" demek. Kavram için Notlarım hub'ında "🧠 Kavram" butonu var.
    PDF render'ı `glossary_notes` listesini ayrıca türetir (WORD + CONCEPT).
    """
    def _impl() -> list[tuple[Note, Book]]:
        with db_session() as s:
            q = (
                select(Note)
                .where(Note.category == Category.WORD)
                .order_by(func.lower(Note.transcript))
            )
            notes = list(s.scalars(q).all())
            results: list[tuple[Note, Book]] = []
            for n in notes:
                b = s.get(Book, n.book_id)
                if b:
                    results.append((n, b))
            return results
    return await asyncio.to_thread(_impl)


async def count_notes_by_category() -> dict[str, int]:
    """Return a dict mapping every Category.name → total note count.

    Counts are populated for ALL categories, including those with zero notes,
    so the UI can render buttons like "Fikir (0)" without surprise KeyErrors.
    """
    def _impl() -> dict[str, int]:
        out: dict[str, int] = {c.name: 0 for c in Category}
        with db_session() as s:
            rows = s.execute(
                select(Note.category, func.count(Note.id)).group_by(Note.category)
            ).all()
            for cat, n in rows:
                # SQLAlchemy may return either the Category enum or its value
                key = cat.name if hasattr(cat, "name") else str(cat)
                out[key] = int(n)
        return out
    return await asyncio.to_thread(_impl)


async def count_notes_by_custom_label() -> dict[str, int]:
    """Counts grouped by Note.category_label (custom user-defined buckets)."""
    def _impl() -> dict[str, int]:
        out: dict[str, int] = {}
        with db_session() as s:
            rows = s.execute(
                select(Note.category_label, func.count(Note.id))
                .where(Note.category_label.isnot(None), Note.category_label != "")
                .group_by(Note.category_label)
            ).all()
            for lbl, n in rows:
                if lbl:
                    out[str(lbl)] = int(n)
        return out
    return await asyncio.to_thread(_impl)


async def notes_by_custom_label(label: str) -> list[tuple[Note, Book]]:
    """All notes whose user-defined category_label matches. Newest first."""
    def _impl() -> list[tuple[Note, Book]]:
        with db_session() as s:
            q = (
                select(Note)
                .where(Note.category_label == label)
                .order_by(Note.created_at.desc())
            )
            notes = list(s.scalars(q).all())
            results: list[tuple[Note, Book]] = []
            for n in notes:
                b = s.get(Book, n.book_id)
                if b:
                    results.append((n, b))
            return results
    return await asyncio.to_thread(_impl)


async def notes_by_category(category: Category) -> list[tuple[Note, Book]]:
    """All notes of a given category, paired with their book. Newest first."""
    def _impl() -> list[tuple[Note, Book]]:
        with db_session() as s:
            q = (
                select(Note)
                .where(Note.category == category)
                .order_by(Note.created_at.desc())
            )
            notes = list(s.scalars(q).all())
            results: list[tuple[Note, Book]] = []
            for n in notes:
                b = s.get(Book, n.book_id)
                if b:
                    results.append((n, b))
            return results
    return await asyncio.to_thread(_impl)


async def list_all_favorites() -> list[tuple[Note, Book]]:
    """All favorited notes across EVERY category. Newest first.

    v1.0.6: Previously `list_quotes(favorites_only=True)` only returned
    QUOTE-category favorites — meaning a user who starred a Kavram or Fikir
    note wouldn't see it in the Telegram "Favoriler" menu. Now this function
    returns all is_favorite=True notes regardless of category.
    """
    def _impl() -> list[tuple[Note, Book]]:
        with db_session() as s:
            q = (
                select(Note)
                .where(Note.is_favorite.is_(True))
                .order_by(Note.created_at.desc())
            )
            notes = list(s.scalars(q).all())
            results: list[tuple[Note, Book]] = []
            for n in notes:
                b = s.get(Book, n.book_id)
                if b:
                    results.append((n, b))
            return results
    return await asyncio.to_thread(_impl)


async def count_all_favorites() -> int:
    """Quick count of is_favorite=True notes (any category)."""
    def _impl() -> int:
        with db_session() as s:
            return int(s.scalar(
                select(func.count(Note.id)).where(Note.is_favorite.is_(True))
            ) or 0)
    return await asyncio.to_thread(_impl)


async def list_quotes(favorites_only: bool = False) -> list[tuple[Note, Book]]:
    """All QUOTE-category notes (optionally favorites only)."""
    def _impl() -> list[tuple[Note, Book]]:
        with db_session() as s:
            q = select(Note).where(Note.category == Category.QUOTE)
            if favorites_only:
                q = q.where(Note.is_favorite.is_(True))
            q = q.order_by(Note.created_at.desc())
            notes = list(s.scalars(q).all())
            results: list[tuple[Note, Book]] = []
            for n in notes:
                b = s.get(Book, n.book_id)
                if b:
                    results.append((n, b))
            return results
    return await asyncio.to_thread(_impl)


async def quotes_for_book(book_id: int) -> list[Note]:
    """All QUOTE notes for a single book (used by the finish ritual)."""
    def _impl() -> list[Note]:
        with db_session() as s:
            q = (
                select(Note)
                .where(Note.book_id == book_id, Note.category == Category.QUOTE)
                .order_by(Note.created_at.asc())
            )
            return list(s.scalars(q).all())
    return await asyncio.to_thread(_impl)


# ─────────────────────────── App settings ───────────────────────────


async def get_settings() -> AppSettings:
    """Fetch the singleton settings row (creating it if missing)."""
    def _impl() -> AppSettings:
        with db_session() as s:
            row = s.get(AppSettings, 1)
            if row is None:
                row = AppSettings(id=1)
                s.add(row)
                s.flush()
                s.refresh(row)
            return row
    return await asyncio.to_thread(_impl)


async def update_settings(**fields: Any) -> AppSettings:
    """Patch settings."""
    def _impl() -> AppSettings:
        with db_session() as s:
            row = s.get(AppSettings, 1) or AppSettings(id=1)
            for k, v in fields.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            row.updated_at = utcnow()
            s.merge(row)
            s.flush()
            return s.get(AppSettings, 1)
    result = await asyncio.to_thread(_impl)
    mark_dirty()
    return result


# ─────────────────────────── Ephemeral chat state ───────────────────────────


async def set_ephemeral(chat_id: int, key: str, value: Any, ttl_s: int = 1800) -> None:
    """Store a JSON-serialisable value with a TTL."""
    expires = utcnow() + timedelta(seconds=ttl_s)
    payload = json.dumps(value, ensure_ascii=False, default=str)

    def _impl() -> None:
        with db_session() as s:
            row = s.get(EphemeralState, (chat_id, key))
            if row is None:
                s.add(EphemeralState(chat_id=chat_id, key=key, value=payload, expires_at=expires))
            else:
                row.value = payload
                row.expires_at = expires
    await asyncio.to_thread(_impl)
    mark_dirty()


async def get_ephemeral(chat_id: int, key: str) -> Any:
    """Read an ephemeral value, deleting it if expired. Returns None if absent."""
    def _impl() -> Any:
        now = utcnow()
        with db_session() as s:
            row = s.get(EphemeralState, (chat_id, key))
            if row is None:
                return None
            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < now:
                s.delete(row)
                return None
            try:
                return json.loads(row.value)
            except Exception:
                return None
    return await asyncio.to_thread(_impl)


async def clear_ephemeral(chat_id: int, *keys: str) -> None:
    """Delete one or more ephemeral keys for a chat."""
    def _impl() -> None:
        with db_session() as s:
            for k in keys:
                row = s.get(EphemeralState, (chat_id, k))
                if row is not None:
                    s.delete(row)
    await asyncio.to_thread(_impl)
    mark_dirty()


async def purge_expired_ephemeral() -> int:
    """Sweep expired rows. Returns count deleted."""
    def _impl() -> int:
        now = utcnow()
        with db_session() as s:
            rows = list(s.scalars(select(EphemeralState).where(EphemeralState.expires_at < now)).all())
            for r in rows:
                s.delete(r)
            return len(rows)
    return await asyncio.to_thread(_impl)


# ─────────────────────────── Statistics ───────────────────────────


async def compute_stats(period_days: int | None = None) -> dict[str, Any]:
    """Detailed reading statistics.

    Returns a dict with raw underlying data so the bot can render it with
    concrete book names / dates rather than aggregate numbers only.
    """
    def _impl() -> dict[str, Any]:
        with db_session() as s:
            now = utcnow()
            cutoff = now - timedelta(days=period_days) if period_days else None

            book_q = select(Book)
            if cutoff:
                # Books "active" in period = had a session in period OR finished in period
                pass  # filter applied per-book below
            books = list(s.scalars(book_q).all())

            sess_q = select(Session).where(Session.ended_at.is_not(None))
            if cutoff:
                sess_q = sess_q.where(Session.started_at >= cutoff)
            sessions = list(s.scalars(sess_q.order_by(Session.started_at.asc())).all())

            note_q = select(Note)
            if cutoff:
                note_q = note_q.where(Note.created_at >= cutoff)
            notes = list(s.scalars(note_q).all())

            finished_in_period = [
                b for b in books
                if b.status == BookStatus.FINISHED
                and (cutoff is None or any(
                    sess.book_id == b.id and sess.ended_at and sess.ended_at >= cutoff
                    for sess in sessions
                ))
            ]

            total_min = sum((sess.duration_min or 0) for sess in sessions)
            session_records: list[dict[str, Any]] = []
            for sess in sessions:
                b = next((bk for bk in books if bk.id == sess.book_id), None)
                started = to_local(sess.started_at)
                ended = to_local(sess.ended_at)
                session_records.append({
                    "code": sess.code,
                    "book_title": b.title if b else "?",
                    "book_short_code": b.short_code if b else "",
                    "started_at": started,
                    "ended_at": ended,
                    "duration_min": sess.duration_min or 0,
                    "start_page": sess.start_page,
                    "end_page": sess.end_page,
                    "hour_of_day": started.hour if started else None,
                })

            # Most productive time-of-day bucket
            bucket_min: dict[str, int] = {
                "Sabah (06-12)": 0,
                "Öğleden sonra (12-18)": 0,
                "Akşam (18-24)": 0,
                "Gece (00-06)": 0,
            }
            bucket_sessions: dict[str, list[dict[str, Any]]] = {k: [] for k in bucket_min}
            for rec in session_records:
                h = rec["hour_of_day"]
                if h is None:
                    continue
                if 6 <= h < 12:
                    key = "Sabah (06-12)"
                elif 12 <= h < 18:
                    key = "Öğleden sonra (12-18)"
                elif 18 <= h < 24:
                    key = "Akşam (18-24)"
                else:
                    key = "Gece (00-06)"
                bucket_min[key] += rec["duration_min"]
                bucket_sessions[key].append(rec)

            best_bucket = max(bucket_min.items(), key=lambda kv: kv[1]) if any(bucket_min.values()) else ("—", 0)

            # Streak (consecutive days with at least one session, ending today/yesterday)
            session_days = sorted({
                to_local(sess.started_at).date()
                for sess in sessions
                if sess.started_at
            }, reverse=True)
            streak = 0
            cursor = now.astimezone(DISPLAY_TZ).date()
            if session_days and (cursor - session_days[0]).days <= 1:
                cursor = session_days[0]
                for d in session_days:
                    if d == cursor:
                        streak += 1
                        cursor = cursor - timedelta(days=1)
                    elif d < cursor:
                        break

            return {
                "period_days": period_days,
                "total_books": len(books),
                "finished_count": sum(1 for b in books if b.status == BookStatus.FINISHED),
                "reading_count": sum(1 for b in books if b.status == BookStatus.READING),
                "paused_count": sum(1 for b in books if b.status == BookStatus.PAUSED),
                "session_count": len(sessions),
                "total_minutes": total_min,
                "total_hours": total_min // 60,
                "total_min_rem": total_min % 60,
                "note_count": len(notes),
                "finished_in_period": [
                    {
                        "title": b.title,
                        "short_code": b.short_code,
                        "total_pages": b.total_pages,
                        "rating": b.rating,
                    }
                    for b in finished_in_period
                ],
                "sessions": session_records,
                "bucket_minutes": bucket_min,
                "bucket_sessions": bucket_sessions,
                "best_bucket": best_bucket,
                "streak_days": streak,
            }
    return await asyncio.to_thread(_impl)


async def books_idle_since(days: int) -> list[Book]:
    """Books in READING status whose most recent session ended (or started)
    more than `days` days ago. Used by the daily nudge."""
    def _impl() -> list[Book]:
        with db_session() as s:
            threshold = utcnow() - timedelta(days=days)
            books = list(s.scalars(select(Book).where(Book.status == BookStatus.READING)).all())
            idle: list[Book] = []
            for b in books:
                last_sess = s.scalars(
                    select(Session)
                    .where(Session.book_id == b.id)
                    .order_by(Session.started_at.desc())
                    .limit(1)
                ).first()
                ref = None
                if last_sess:
                    ref = last_sess.ended_at or last_sess.started_at
                if ref is None or ref < threshold.replace(tzinfo=None):
                    idle.append(b)
            return idle
    return await asyncio.to_thread(_impl)


# ─────────────────────────── PDF + Exports ───────────────────────────


_jinja_env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html"]),
)


# Locale-independent Turkish date formatting for the PDF journal.
# `strftime('%B'/'%b')` is locale-bound and renders English month names
# ("March", "May") under the Cloud Run container's default C locale, which
# looks unfinished in an otherwise-Turkish journal. This filter substitutes
# Turkish month names before delegating the rest to strftime.
_TR_MONTHS = [
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]


def _tr_date(dt: Any, fmt: str = "%d %B %Y") -> str:
    """Format a datetime with Turkish month names, locale-independent.

    Both ``%B`` and ``%b`` resolve to the full Turkish month name — Turkish
    month names are already short (≤ 7 chars) and the official TDK three-letter
    abbreviations ("May") visually collide with English, undermining the
    finished-product feel of the journal.
    """
    if dt is None:
        return ""
    name = _TR_MONTHS[dt.month - 1]
    resolved = fmt.replace("%B", name).replace("%b", name)
    return dt.strftime(resolved)


_jinja_env.filters["tr_date"] = _tr_date


# v1.0.6 — Kitabi logosu PDF render'ında inline (base64 data URI) olarak
# template'e geçer. Container'da /app/kitabilogo.png olarak; lokal dev'de
# paket kökünün bir üstünde. İlk okumada cache'lenir.
_LOGO_BASE_PATHS = [
    Path("/app/kitabilogo.png"),
    Path(__file__).resolve().parent.parent / "kitabilogo.png",
]
_LOGO_BASE_WHITE_PATHS = [
    Path("/app/kitabilogo-white.png"),
    Path(__file__).resolve().parent.parent / "kitabilogo-white.png",
]
_kitabi_logo_uri_cache: str | None = None
_kitabi_logo_white_uri_cache: str | None = None


def _file_to_data_uri(paths: list[Path]) -> str | None:
    """Read the first existing file from `paths` and return a data: URI."""
    import base64
    for p in paths:
        if p.exists():
            try:
                blob = p.read_bytes()
                b64 = base64.b64encode(blob).decode("ascii")
                return f"data:image/png;base64,{b64}"
            except Exception as e:
                logger.warning("data.logo.read_failed", path=str(p), error=str(e))
    return None


def _load_kitabi_logo_uri() -> str | None:
    """Cache-on-first-call: load kitabilogo.png as a data URI."""
    global _kitabi_logo_uri_cache
    if _kitabi_logo_uri_cache is None:
        _kitabi_logo_uri_cache = _file_to_data_uri(_LOGO_BASE_PATHS)
    return _kitabi_logo_uri_cache


def _load_kitabi_logo_white_uri() -> str | None:
    """Cache-on-first-call: load kitabilogo-white.png as a data URI.

    The white variant is used on the dark cover page where the black logo
    wouldn't be readable.
    """
    global _kitabi_logo_white_uri_cache
    if _kitabi_logo_white_uri_cache is None:
        _kitabi_logo_white_uri_cache = _file_to_data_uri(_LOGO_BASE_WHITE_PATHS)
    return _kitabi_logo_white_uri_cache


def _compute_book_stats(
    book: Book, sessions: list[Session], notes: list[Note]
) -> dict[str, Any]:
    """Aggregate stats shown on the PDF stats page."""
    total_min = sum((sess.duration_min or 0) for sess in sessions)
    category_counts: dict[str, int] = {cat.value: 0 for cat in Category}
    for note in notes:
        category_counts[note.category.value] += 1

    # Pages read across sessions (delta of end_page - start_page summed)
    pages_read = 0
    for sess in sessions:
        if sess.start_page and sess.end_page and sess.end_page > sess.start_page:
            pages_read += sess.end_page - sess.start_page

    return {
        "session_count": len(sessions),
        "total_minutes": total_min,
        "hours": total_min // 60,
        "minutes_remainder": total_min % 60,
        "note_count": len(notes),
        "category_counts": category_counts,
        "pages_read": pages_read,
        "word_count":    sum(1 for n in notes if n.category == Category.WORD),
        "concept_count": sum(1 for n in notes if n.category == Category.CONCEPT),
        "quote_count":   sum(1 for n in notes if n.category == Category.QUOTE),
        "idea_count":    sum(1 for n in notes if n.category == Category.IDEA),
        "started_at": to_local(sessions[0].started_at) if sessions else None,
        "ended_at": (
            to_local(sessions[-1].ended_at) if sessions and sessions[-1].ended_at else None
        ),
        "calendar": _build_reading_calendar(sessions),
    }


def _build_reading_calendar(sessions: list[Session]) -> list[dict[str, Any]]:
    """For each month spanned by the sessions, build a calendar grid showing
    which days had reading activity. Returns one dict per month:

        {
          "label": "Mart 2026",
          "year": 2026, "month": 3,
          "first_weekday": 0,        # 0=Monday … 6=Sunday
          "days_in_month": 31,
          "active_days": {3, 7, 14}, # days with ≥1 session that month
          "minutes_per_day": {3: 45, 7: 30, …},
        }
    """
    if not sessions:
        return []
    import calendar as _cal
    months: dict[tuple[int, int], dict[str, Any]] = {}
    tr_months = [
        "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
        "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
    ]
    for sess in sessions:
        local = to_local(sess.started_at)
        if local is None:
            continue
        key = (local.year, local.month)
        if key not in months:
            first_weekday = _cal.monthrange(local.year, local.month)[0]
            days_in_month = _cal.monthrange(local.year, local.month)[1]
            months[key] = {
                "label": f"{tr_months[local.month - 1]} {local.year}",
                "year": local.year,
                "month": local.month,
                "first_weekday": first_weekday,
                "days_in_month": days_in_month,
                "active_days": set(),
                "minutes_per_day": {},
            }
        m = months[key]
        m["active_days"].add(local.day)
        m["minutes_per_day"][local.day] = (
            m["minutes_per_day"].get(local.day, 0) + (sess.duration_min or 0)
        )
    # Convert sets to sorted lists for Jinja serialization
    out: list[dict[str, Any]] = []
    for key in sorted(months.keys()):
        m = months[key]
        m["active_days"] = sorted(m["active_days"])
        out.append(m)
    return out


# ─────────────────────────── Note share — Classic Twit ───────────────────────────
#
# v1.0.3 redesign: "Klasik Twit / Pull-quote" layout.
# Layout is fixed:  brand bar (top) → big "  quote mark → body → meta block (bottom)
# with the GitHub URL in a tiny footer. Body font auto-shrinks based on
# transcript length so the OUTSIDE PADDING NEVER CHANGES — long quotes just
# get smaller type. Available fonts mirror the Telegram font picker.

KITABI_GITHUB_URL = "github.com/poeple-app/kitabi"


# Each option: (display_label, css font-family stack, weight, optional @font-face URL)
# The CSS stack falls back to system serifs so renders work even if WeasyPrint
# can't fetch the Google Fonts CDN entry.
NOTE_SHARE_FONTS: dict[str, dict[str, str]] = {
    "crimson": {
        "label":   "Crimson Pro (varsayılan)",
        "family":  "'Crimson Pro', 'EB Garamond', Georgia, serif",
        "google":  "https://fonts.googleapis.com/css2?family=Crimson+Pro:ital,wght@0,400;0,500;0,700;1,400&display=swap",
    },
    "playfair": {
        "label":   "Playfair Display (cüretkar)",
        "family":  "'Playfair Display', 'Bodoni 72', Georgia, serif",
        "google":  "https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;0,900;1,400&display=swap",
    },
    "cormorant": {
        "label":   "Cormorant Garamond (klasik)",
        "family":  "'Cormorant Garamond', 'EB Garamond', Garamond, serif",
        "google":  "https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,700;1,400&display=swap",
    },
    "ebgaramond": {
        "label":   "EB Garamond (kitap)",
        "family":  "'EB Garamond', Garamond, Georgia, serif",
        "google":  "",  # Debian'da fonts-ebgaramond olarak yüklü
    },
    "lora": {
        "label":   "Lora (web-optimize)",
        "family":  "'Lora', 'Source Serif Pro', Georgia, serif",
        # Lora Debian Trixie'de yok → CDN'den çek
        "google":  "https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,500;0,700;1,400&display=swap",
    },
    "merriweather": {
        "label":   "Merriweather (okunaklı)",
        "family":  "'Merriweather', Georgia, serif",
        "google":  "https://fonts.googleapis.com/css2?family=Merriweather:ital,wght@0,400;0,700;1,400&display=swap",
    },
}


def _pick_body_font_size_pt(transcript: str, fmt: str) -> tuple[int, int]:
    """Choose body font-size + quote-mark size based on length, so the OUTER
    boundaries of the card never change. Returns (body_pt, mark_pt).

    Square/A5 are tighter than story/A4, so we use a slightly different curve.
    """
    n = len(transcript or "")
    tall = fmt in ("story", "a4")
    # Buckets, tuned by eye against the mockup's 27pt at ~150 chars
    if n <= 120:
        return (32 if tall else 28), 140
    if n <= 220:
        return (28 if tall else 24), 130
    if n <= 380:
        return (24 if tall else 20), 110
    if n <= 600:
        return (20 if tall else 17), 90
    if n <= 900:
        return (17 if tall else 14), 70
    # Very long
    return (14 if tall else 12), 56


async def render_note_share(
    *, note: Note, book: Book | None, fmt: str, user_name: str,
    font_key: str = "crimson",
) -> tuple[bytes, str, str]:
    """Render a single note as a shareable PDF in the "Klasik Twit" style.

    `fmt` ∈ {square, post, story, a4, a5}. `font_key` ∈ NOTE_SHARE_FONTS.

    Returns (bytes, filename, mime_type). All formats produce a 1-page PDF; the
    user takes a screenshot of page 1 to share. Body font auto-shrinks so the
    fixed outer padding never gets violated — long quotes just get smaller type.
    """
    sizes_mm = {
        # ~96 dpi pixel canvases — px ≈ mm * 3.78
        "square": ("285mm", "285mm"),   # 1080x1080
        "post":   ("285mm", "285mm"),
        "story":  ("285mm", "508mm"),   # 1080x1920
        "a4":     ("210mm", "297mm"),
        "a5":     ("148mm", "210mm"),
    }
    if fmt not in sizes_mm:
        fmt = "square"
    w, h = sizes_mm[fmt]
    fmt_label = {
        "square": "Kare", "post": "Instagram Post",
        "story": "Instagram Story", "a4": "A4", "a5": "A5",
    }[fmt]

    font = NOTE_SHARE_FONTS.get(font_key) or NOTE_SHARE_FONTS["crimson"]
    body_pt, mark_pt = _pick_body_font_size_pt(note.transcript or "", fmt)

    book_title = (book.title if book else "(kitap)")
    book_author = book.author if book and book.author else None
    short_code = book.short_code if book else ""
    page_s = note.page if note.page is not None else "—"
    created = to_local(note.created_at)
    date_str = created.strftime("%d %B %Y") if created else ""

    transcript_html = (note.transcript or "").replace("\n", "<br/>")

    # ── HTML escape only inside the rendered text spots ────────────
    def _esc(x: object) -> str:
        return _html.escape(str(x) if x is not None else "")

    google_import = ""
    if font.get("google"):
        google_import = f"@import url('{font['google']}');\n"

    # Padding scales with the format so tall pages don't look sparse
    px = "22mm" if fmt in ("story", "a4") else "18mm"

    css = f"""
    {google_import}
    @page {{ size: {w} {h}; margin: 0; }}
    body {{
      margin: 0; padding: 0;
      width: {w}; height: {h};
      font-family: {font['family']};
      color: #2a2520;
      background: #fdfaf3;
      background-image:
        radial-gradient(circle at 15% 18%, rgba(122,62,31,.04), transparent 50%),
        radial-gradient(circle at 88% 82%, rgba(122,62,31,.06), transparent 50%);
    }}
    .wrap {{
      box-sizing: border-box;
      padding: {px};
      width: 100%; height: 100%;
      display: flex; flex-direction: column; justify-content: space-between;
    }}
    /* ─── Brand bar (top) ─── */
    .brand-bar {{
      display: flex; justify-content: space-between; align-items: center;
      font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif;
      font-size: 9pt;
      letter-spacing: 4px;
      text-transform: uppercase;
      color: #8a7e72;
    }}
    .brand-bar b {{
      color: #7a3e1f;
      font-weight: 700;
      letter-spacing: 5px;
    }}
    /* ─── Quote block (middle) ─── */
    .quote-section {{
      flex: 1;
      display: flex; flex-direction: column;
      justify-content: center;
      padding: 4mm 0;
    }}
    .quote-mark {{
      font-family: 'Playfair Display', Georgia, serif;
      font-size: {mark_pt}pt;
      color: #7a3e1f;
      line-height: 0;
      height: {int(mark_pt * 0.55)}pt;
      margin-bottom: 2mm;
    }}
    .quote-body {{
      font-size: {body_pt}pt;
      line-height: 1.42;
      font-weight: 500;
      color: #2a2520;
      letter-spacing: -0.2px;
    }}
    /* ─── Meta block (bottom) ─── */
    .meta-block {{
      font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif;
    }}
    .meta-block .book-title {{
      font-family: {font['family']};
      font-size: 14pt;
      font-weight: 700;
      color: #2a2520;
      letter-spacing: -0.3px;
    }}
    .meta-block .book-author {{
      font-size: 11pt;
      font-style: italic;
      color: #6e6358;
      margin-top: 1mm;
    }}
    .meta-row {{
      display: flex; justify-content: space-between;
      align-items: center;
      margin-top: 4mm;
      padding-top: 3mm;
      border-top: 1px solid #d8cdba;
      font-size: 8.5pt;
      color: #8a7e72;
      letter-spacing: 0.5px;
    }}
    .gh {{
      margin-top: 3mm;
      text-align: center;
      font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif;
      font-size: 7.5pt;
      letter-spacing: 1.5px;
      color: #b5a896;
    }}
    """

    author_html = (
        f'<div class="book-author">{_esc(book_author)}</div>' if book_author else ""
    )
    who_html = f" — {_esc(user_name)}" if user_name else ""
    html_str = f"""<!doctype html>
    <html lang="tr"><head><meta charset="utf-8"><style>{css}</style></head>
    <body>
      <div class="wrap">
        <div class="brand-bar">
          <b>Kitabi</b>
          <span>okuma günlüğü</span>
        </div>

        <div class="quote-section">
          <div class="quote-mark">&ldquo;</div>
          <div class="quote-body">{transcript_html}</div>
        </div>

        <div>
          <div class="meta-block">
            <div class="book-title">{_esc(book_title)}</div>
            {author_html}
            <div class="meta-row">
              <span>s.{_esc(page_s)} · {_esc(note.code)}</span>
              <span>{_esc(date_str)}{who_html}</span>
            </div>
          </div>
          <div class="gh">{KITABI_GITHUB_URL}</div>
        </div>
      </div>
    </body></html>
    """

    def _impl() -> bytes:
        return HTML(string=html_str, base_url="templates").write_pdf()
    pdf_bytes = await asyncio.to_thread(_impl)
    safe_fname = "".join(c if c.isalnum() else "_" for c in book_title)[:40] or "not"
    return pdf_bytes, f"{safe_fname}-{note.code}-{fmt}.pdf", "application/pdf"


async def render_pdf(book_id: int) -> bytes:
    """Generate the reading-journal PDF for a book.

    v1.0.2: lazily fetches the author's 3 other well-known works via Gemini
    if the book doesn't already have them cached in `author_other_books`,
    and persists the result for next time.
    """
    t0 = time.time()
    logger.info("data.render_pdf.start", book_id=book_id)

    # Step 1 (off-thread): make sure author_other_books is populated.
    # We do this before the sync render block so we can await the AI call.
    book_quick = await get_book(book_id)
    if not book_quick:
        raise ValueError(f"Book {book_id} not found")
    cached_aob = getattr(book_quick, "author_other_books", None) or []
    if (not cached_aob) and book_quick.author:
        try:
            from . import ai as _ai
            others = await _ai.list_author_other_books(book_quick.author, book_quick.title)
            if others:
                await update_book(book_id, author_other_books=others)
        except Exception as e:
            logger.warning("data.render_pdf.author_other_books_failed", error=str(e))

    # Step 2 (off-thread): download Telegram photos for any note that carries
    # a photo_file_id. Cached into note_id → data: URI map so the Jinja
    # template can embed them inline. Failures fall back to a placeholder.
    photo_data_uris: dict[int, str] = {}
    def _ids_with_photos() -> list[tuple[int, str]]:
        with db_session() as s:
            rows = s.execute(
                select(Note.id, Note.photo_file_id)
                .where(Note.book_id == book_id, Note.photo_file_id.isnot(None))
            ).all()
            return [(int(nid), str(fid)) for nid, fid in rows if fid]
    pending_photos = await asyncio.to_thread(_ids_with_photos)
    if pending_photos and _telegram_bot_token:
        logger.info("data.render_pdf.fetching_photos", count=len(pending_photos))
        # Concurrent fetch (Cloud Run egress is generous; this is bounded by note count)
        results = await asyncio.gather(
            *[_fetch_telegram_photo_b64(fid) for _, fid in pending_photos],
            return_exceptions=True,
        )
        for (nid, _), result in zip(pending_photos, results):
            if isinstance(result, str):
                photo_data_uris[nid] = result

    def _impl() -> bytes:
        with db_session() as s:
            book = s.get(Book, book_id)
            if not book:
                raise ValueError(f"Book {book_id} not found")
            sessions = sorted(book.sessions, key=lambda x: x.started_at)
            notes = sorted(book.notes, key=lambda x: x.created_at)
            favorites = [n for n in notes if n.is_favorite]
            glossary_notes = [
                n for n in notes if n.category in (Category.WORD, Category.CONCEPT)
            ]
            # v1.0.5 — Tag cloud "kelime" odaklı (cümle yok, hep aynı boyut).
            # Notion multi-select tag mantığı. Cümle gibi uzun girişler (≥30
            # karakter veya >3 kelime) cloud dışında bırakılır; klasik
            # alfabetik liste onları zaten gösterir.
            glossary_cloud = []
            for n in glossary_notes:
                term = (n.transcript or "").strip()
                if not term:
                    continue
                if len(term) > 30:
                    continue
                if len(term.split()) > 3:
                    continue
                glossary_cloud.append({
                    "term": term,
                    "category": n.category.value,
                    "note_code": n.code,
                })

            template = _jinja_env.get_template("journal.html")
            html_str = template.render(
                book=book,
                sessions=sessions,
                notes=notes,
                Category=Category,
                glossary=glossary_notes,
                glossary_cloud=glossary_cloud,
                summaries=[n for n in notes if n.category == Category.SUMMARY],
                favorites=favorites,
                stats=_compute_book_stats(book, sessions, notes),
                author_other_books=list(getattr(book, "author_other_books", None) or []),
                extra_fields=dict(getattr(book, "extra_fields", None) or {}),
                photo_data_uris=photo_data_uris,
                kitabi_logo_uri=_load_kitabi_logo_uri(),
                kitabi_logo_white_uri=_load_kitabi_logo_white_uri(),
                generated_at=to_local(utcnow()),
                to_local=to_local,
            )
            pdf_bytes = HTML(string=html_str, base_url="templates").write_pdf()
            return pdf_bytes

    result = await asyncio.to_thread(_impl)
    logger.info(
        "data.render_pdf.success",
        book_id=book_id,
        bytes=len(result),
        duration_ms=int((time.time() - t0) * 1000),
    )
    return result


async def export_json(book_id: int) -> bytes:
    """Serialize a book and all its sessions/notes to JSON."""
    def _impl() -> bytes:
        with db_session() as s:
            book = s.get(Book, book_id)
            if not book:
                raise ValueError(f"Book {book_id} not found")
            payload = {
                "book": _book_to_dict(book),
                "sessions": [_session_to_dict(x) for x in book.sessions],
                "notes": [_note_to_dict(x) for x in book.notes],
            }
            return json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    return await asyncio.to_thread(_impl)


async def export_csv(book_id: int) -> bytes:
    """Export notes for a book as CSV (Excel-friendly with BOM)."""
    def _impl() -> bytes:
        with db_session() as s:
            book = s.get(Book, book_id)
            if not book:
                raise ValueError(f"Book {book_id} not found")
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                [
                    "code",
                    "created_at",
                    "category",
                    "page",
                    "transcript",
                    "definition",
                    "explanation",
                    "is_favorite",
                    "from_qa",
                ]
            )
            for n in sorted(book.notes, key=lambda x: x.created_at):
                writer.writerow(
                    [
                        n.code,
                        (to_local(n.created_at) or "").isoformat() if n.created_at else "",
                        n.category.value,
                        n.page or "",
                        n.transcript,
                        n.definition or "",
                        n.explanation or "",
                        "yes" if n.is_favorite else "no",
                        "yes" if n.from_qa else "no",
                    ]
                )
            return buf.getvalue().encode("utf-8-sig")
    return await asyncio.to_thread(_impl)


async def export_markdown(book_id: int) -> bytes:
    """Render a book as Markdown (Obsidian / NotebookLM friendly)."""
    def _impl() -> bytes:
        with db_session() as s:
            book = s.get(Book, book_id)
            if not book:
                raise ValueError(f"Book {book_id} not found")
            lines: list[str] = []
            lines.append(f"# {book.title} ({book.short_code})")
            if book.author:
                lines.append(f"*{book.author}*")
            lines.append("")
            if book.isbn:
                lines.append(f"- ISBN: {book.isbn}")
            if book.genre:
                lines.append(f"- Genre: {book.genre}")
            if book.total_pages:
                lines.append(f"- Pages: {book.read_pages}/{book.total_pages}")
            if book.tags:
                lines.append(f"- Tags: {', '.join(book.tags)}")
            if book.rating:
                lines.append(f"- Rating: {'⭐' * book.rating}")
            if book.one_line_review:
                lines.append(f"- Review: {book.one_line_review}")
            if book.personal_note:
                lines.append("")
                lines.append(f"> {book.personal_note}")
            lines.append("")
            lines.append("## Notes")
            lines.append("")
            for n in sorted(book.notes, key=lambda x: x.created_at):
                ts = to_local(n.created_at).strftime("%Y-%m-%d %H:%M") if n.created_at else ""
                fav = " ⭐" if n.is_favorite else ""
                lines.append(f"### [{n.code}] [{n.category.value}] s.{n.page or '—'}  ({ts}){fav}")
                lines.append("")
                lines.append(n.transcript)
                if n.definition:
                    lines.append("")
                    lines.append(f"*Definition:* {n.definition}")
                if n.explanation:
                    lines.append("")
                    lines.append(f"*Explanation:* {n.explanation}")
                lines.append("")
            return "\n".join(lines).encode("utf-8")
    return await asyncio.to_thread(_impl)


async def export_all_zip() -> bytes:
    """Bundle every book's PDF + JSON + Markdown into a single ZIP archive."""
    books = await list_books()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for book in books:
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in book.title)
            try:
                zf.writestr(f"{safe}/{safe}.pdf", await render_pdf(book.id))
            except Exception as e:
                logger.warning("data.export_all_zip.pdf_failed", book_id=book.id, error=str(e))
            try:
                zf.writestr(f"{safe}/{safe}.json", await export_json(book.id))
            except Exception as e:
                logger.warning("data.export_all_zip.json_failed", book_id=book.id, error=str(e))
            try:
                zf.writestr(f"{safe}/{safe}.md", await export_markdown(book.id))
            except Exception as e:
                logger.warning("data.export_all_zip.md_failed", book_id=book.id, error=str(e))
    return buf.getvalue()


# ─────────────────────────── Book metadata lookup (Google Books + Open Library fallback) ───────────────────────────


GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"
OPENLIBRARY_ISBN_API = "https://openlibrary.org/api/books"
OPENLIBRARY_SEARCH_API = "https://openlibrary.org/search.json"


def normalize_isbn(text: str | None) -> str | None:
    """Strip dashes/spaces from an ISBN, validate it's 10 or 13 digits (X allowed
    only as last digit of ISBN-10). Returns the clean ISBN or None if invalid.
    """
    if not text:
        return None
    cleaned = "".join(c for c in str(text) if c.isdigit() or c.upper() == "X")
    cleaned = cleaned.upper()
    # ISBN-13: all digits; ISBN-10: 9 digits + (digit|X)
    if len(cleaned) == 13 and cleaned.isdigit():
        return cleaned
    if len(cleaned) == 10 and cleaned[:9].isdigit() and (cleaned[9].isdigit() or cleaned[9] == "X"):
        return cleaned
    return None


def _gb_to_metadata(info: dict[str, Any], fallback_isbn: str | None = None) -> dict[str, Any]:
    """Map a Google Books `volumeInfo` payload to our metadata dict shape."""
    authors = info.get("authors") or []
    image_links = info.get("imageLinks") or {}
    categories = info.get("categories") or []
    isbn = fallback_isbn
    for ident in info.get("industryIdentifiers", []) or []:
        if ident.get("type") == "ISBN_13":
            isbn = ident.get("identifier") or isbn
            break
        if ident.get("type") == "ISBN_10":
            isbn = isbn or ident.get("identifier")
    cover_url = (
        image_links.get("extraLarge") or image_links.get("large")
        or image_links.get("medium") or image_links.get("thumbnail")
        or image_links.get("smallThumbnail")
    )
    if cover_url and cover_url.startswith("http://"):
        cover_url = "https://" + cover_url[len("http://"):]
    pub_year = None
    pubdate = info.get("publishedDate") or ""
    if pubdate[:4].isdigit():
        pub_year = int(pubdate[:4])
    return {
        "title": info.get("title") or "",
        "author": ", ".join(authors) if authors else None,
        "genre": categories[0] if categories else None,
        "subgenre": categories[1] if len(categories) > 1 else None,
        "total_pages": info.get("pageCount"),
        "cover_url": cover_url,
        "isbn": isbn,
        "publisher": info.get("publisher"),
        "publication_year": pub_year,
        "goodreads_url": None,
    }


def _ol_isbn_to_metadata(entry: dict[str, Any], isbn: str) -> dict[str, Any]:
    """Map an Open Library `?jscmd=data` payload for a single book to our shape."""
    authors_list = entry.get("authors") or []
    author = ", ".join(a.get("name", "") for a in authors_list if a.get("name")) or None
    subjects = entry.get("subjects") or []
    genre = subjects[0].get("name") if subjects and isinstance(subjects[0], dict) else None
    subgenre = subjects[1].get("name") if len(subjects) > 1 and isinstance(subjects[1], dict) else None
    cover = entry.get("cover") or {}
    cover_url = cover.get("large") or cover.get("medium") or cover.get("small")
    if cover_url and cover_url.startswith("http://"):
        cover_url = "https://" + cover_url[len("http://"):]
    publishers = entry.get("publishers") or []
    publisher = (
        publishers[0].get("name") if publishers and isinstance(publishers[0], dict)
        else None
    )
    pub_year = None
    pubdate = entry.get("publish_date") or ""
    for tok in pubdate.split():
        if tok.isdigit() and len(tok) == 4:
            pub_year = int(tok); break
    return {
        "title": entry.get("title") or "",
        "author": author,
        "genre": genre,
        "subgenre": subgenre,
        "total_pages": entry.get("number_of_pages"),
        "cover_url": cover_url,
        "isbn": isbn,
        "publisher": publisher,
        "publication_year": pub_year,
        "goodreads_url": None,
    }


def _ol_search_doc_to_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """Map an Open Library search result doc to our metadata shape."""
    isbn_list = doc.get("isbn") or []
    isbn = next((i for i in isbn_list if len(str(i)) == 13), None) or (isbn_list[0] if isbn_list else None)
    cover_id = doc.get("cover_i")
    cover_url = (
        f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None
    )
    authors = doc.get("author_name") or []
    subjects = doc.get("subject") or []
    publishers = doc.get("publisher") or []
    return {
        "title": doc.get("title") or "",
        "author": ", ".join(authors) if authors else None,
        "genre": subjects[0] if subjects else None,
        "subgenre": subjects[1] if len(subjects) > 1 else None,
        "total_pages": doc.get("number_of_pages_median"),
        "cover_url": cover_url,
        "isbn": isbn,
        "publisher": publishers[0] if publishers else None,
        "publication_year": doc.get("first_publish_year"),
        "goodreads_url": None,
    }


async def _google_books_by_isbn(isbn_clean: str) -> dict[str, Any] | None:
    """Single Google Books ISBN call. Returns metadata dict or None."""
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                GOOGLE_BOOKS_API,
                params={"q": f"isbn:{isbn_clean}", "maxResults": 1},
            )
            r.raise_for_status()
            payload = r.json()
    except Exception as e:
        logger.warning(
            "data.lookup_book.google_books_failed",
            isbn=isbn_clean, error=str(e),
            duration_ms=int((time.time() - t0) * 1000),
        )
        return None
    items = payload.get("items") or []
    if not items:
        logger.info("data.lookup_book.google_books_empty", isbn=isbn_clean)
        return None
    return _gb_to_metadata(items[0].get("volumeInfo", {}), fallback_isbn=isbn_clean)


async def _openlibrary_by_isbn(isbn_clean: str) -> dict[str, Any] | None:
    """Open Library ISBN call. Used as fallback when Google Books fails/empty."""
    t0 = time.time()
    key = f"ISBN:{isbn_clean}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                OPENLIBRARY_ISBN_API,
                params={"bibkeys": key, "format": "json", "jscmd": "data"},
            )
            r.raise_for_status()
            payload = r.json() or {}
    except Exception as e:
        logger.warning(
            "data.lookup_book.openlibrary_failed",
            isbn=isbn_clean, error=str(e),
            duration_ms=int((time.time() - t0) * 1000),
        )
        return None
    entry = payload.get(key)
    if not entry:
        logger.info("data.lookup_book.openlibrary_empty", isbn=isbn_clean)
        return None
    return _ol_isbn_to_metadata(entry, isbn_clean)


async def lookup_book_metadata(isbn: str) -> dict[str, Any] | None:
    """Fetch book metadata by ISBN with Google Books → Open Library fallback.

    Returns None only if BOTH sources fail or return empty. Handles 429 / network
    errors on Google by transparently falling back to Open Library.
    """
    isbn_clean = normalize_isbn(isbn)
    if not isbn_clean:
        logger.warning("data.lookup_book.invalid_isbn", isbn=isbn)
        return None

    logger.info("data.lookup_book.start", isbn=isbn_clean)
    meta = await _google_books_by_isbn(isbn_clean)
    if meta:
        logger.info("data.lookup_book.found", isbn=isbn_clean, source="google_books")
        return meta
    meta = await _openlibrary_by_isbn(isbn_clean)
    if meta:
        logger.info("data.lookup_book.found", isbn=isbn_clean, source="openlibrary")
        return meta
    logger.info("data.lookup_book.not_found_anywhere", isbn=isbn_clean)
    return None


async def _google_books_by_title_author(
    title: str | None, author: str | None
) -> dict[str, Any] | None:
    q_parts: list[str] = []
    if title:
        q_parts.append(f'intitle:"{title}"')
    if author:
        q_parts.append(f'inauthor:"{author}"')
    query = " ".join(q_parts)
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(GOOGLE_BOOKS_API, params={"q": query, "maxResults": 1})
            r.raise_for_status()
            payload = r.json()
    except Exception as e:
        logger.warning("data.lookup_by_title.google_books_failed", error=str(e),
                       duration_ms=int((time.time() - t0) * 1000))
        return None
    items = payload.get("items") or []
    if not items:
        logger.info("data.lookup_by_title.google_books_empty")
        return None
    meta = _gb_to_metadata(items[0].get("volumeInfo", {}))
    # Fill the original input if Google left a field empty
    if not meta.get("title") and title:
        meta["title"] = title
    if not meta.get("author") and author:
        meta["author"] = author
    return meta


async def _openlibrary_by_title_author(
    title: str | None, author: str | None
) -> dict[str, Any] | None:
    params: dict[str, Any] = {"limit": 1}
    if title:
        params["title"] = title
    if author:
        params["author"] = author
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(OPENLIBRARY_SEARCH_API, params=params)
            r.raise_for_status()
            payload = r.json() or {}
    except Exception as e:
        logger.warning("data.lookup_by_title.openlibrary_failed", error=str(e),
                       duration_ms=int((time.time() - t0) * 1000))
        return None
    docs = payload.get("docs") or []
    if not docs:
        logger.info("data.lookup_by_title.openlibrary_empty")
        return None
    meta = _ol_search_doc_to_metadata(docs[0])
    if not meta.get("title") and title:
        meta["title"] = title
    if not meta.get("author") and author:
        meta["author"] = author
    return meta


async def lookup_book_by_title_author(
    title: str | None, author: str | None = None
) -> dict[str, Any] | None:
    """Title+author search with Google Books → Open Library fallback.

    Used when cover-photo extraction yields a title (and maybe author) but no
    ISBN, or when ISBN lookup fails. Same return shape as `lookup_book_metadata`.
    """
    if not title and not author:
        return None
    logger.info("data.lookup_by_title.start", title=title, author=author)
    meta = await _google_books_by_title_author(title, author)
    if meta:
        logger.info("data.lookup_by_title.found", source="google_books")
        return meta
    meta = await _openlibrary_by_title_author(title, author)
    if meta:
        logger.info("data.lookup_by_title.found", source="openlibrary")
        return meta
    logger.info("data.lookup_by_title.not_found_anywhere")
    return None


# ─────────────────────────── Serialization helpers ───────────────────────────


def _book_to_dict(b: Book) -> dict[str, Any]:
    return {
        "id": b.id,
        "short_code": b.short_code,
        "title": b.title,
        "author": b.author,
        "isbn": b.isbn,
        "publisher": getattr(b, "publisher", None),
        "publication_year": getattr(b, "publication_year", None),
        "genre": b.genre,
        "subgenre": b.subgenre,
        "total_pages": b.total_pages,
        "read_pages": b.read_pages,
        "status": b.status.value if b.status else None,
        "cover_url": b.cover_url,
        "icon": getattr(b, "icon", "📖"),
        "bought_from": b.bought_from,
        "price_tl": b.price_tl,
        "bought_at": b.bought_at.isoformat() if b.bought_at else None,
        "tags": b.tags,
        "personal_note": b.personal_note,
        "rating": b.rating,
        "one_line_review": b.one_line_review,
        "would_recommend": b.would_recommend,
        "goodreads_url": b.goodreads_url,
        "author_other_books": getattr(b, "author_other_books", []) or [],
        "extra_fields": getattr(b, "extra_fields", {}) or {},
        "shelf_id": getattr(b, "shelf_id", None),
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


def _session_to_dict(s: Session) -> dict[str, Any]:
    return {
        "id": s.id,
        "code": s.code,
        "book_id": s.book_id,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "start_page": s.start_page,
        "end_page": s.end_page,
        "duration_min": s.duration_min,
    }


def _note_to_dict(n: Note) -> dict[str, Any]:
    return {
        "id": n.id,
        "code": n.code,
        "book_id": n.book_id,
        "session_id": n.session_id,
        "category": n.category.value if n.category else None,
        "page": n.page,
        "transcript": n.transcript,
        "definition": n.definition,
        "explanation": n.explanation,
        "is_favorite": n.is_favorite,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "from_qa": n.from_qa,
        "photo_file_id": getattr(n, "photo_file_id", None),
        "is_orphan_photo": getattr(n, "is_orphan_photo", False),
    }


# ─────────────────────────── Shelf CRUD (v1.0.2) ───────────────────────────


async def list_shelves() -> list[Shelf]:
    """Return all shelves, oldest first."""
    def _impl() -> list[Shelf]:
        with db_session() as s:
            return list(
                s.scalars(select(Shelf).order_by(Shelf.created_at.asc())).all()
            )
    return await asyncio.to_thread(_impl)


async def get_shelf(shelf_id: int) -> Shelf | None:
    def _impl() -> Shelf | None:
        with db_session() as s:
            return s.get(Shelf, shelf_id)
    return await asyncio.to_thread(_impl)


async def create_shelf(name: str, icon: str = "📚") -> Shelf:
    def _impl() -> Shelf:
        with db_session() as s:
            sh = Shelf(name=name.strip()[:80], icon=(icon or "📚")[:8])
            s.add(sh)
            s.flush()
            s.refresh(sh)
            return sh
    out = await asyncio.to_thread(_impl)
    mark_dirty()
    return out


async def delete_shelf(shelf_id: int) -> bool:
    """Delete a shelf. Books remain (their shelf_id becomes NULL via FK)."""
    def _impl() -> bool:
        with db_session() as s:
            sh = s.get(Shelf, shelf_id)
            if sh is None:
                return False
            s.delete(sh)
            return True
    ok = await asyncio.to_thread(_impl)
    mark_dirty()
    return ok


async def books_in_shelf(shelf_id: int | None) -> list[Book]:
    """Return books on a shelf. Pass `shelf_id=None` for books without a shelf."""
    def _impl() -> list[Book]:
        with db_session() as s:
            q = select(Book)
            if shelf_id is None:
                q = q.where(Book.shelf_id.is_(None))
            else:
                q = q.where(Book.shelf_id == shelf_id)
            return list(s.scalars(q.order_by(Book.title.asc())).all())
    return await asyncio.to_thread(_impl)


# ─────────────────────────── Book extra-fields helpers (v1.0.2) ──────────────


async def set_book_extra_field(book_id: int, key: str, value: str) -> None:
    """Add or update a user-defined extra field on a book."""
    def _impl() -> None:
        with db_session() as s:
            b = s.get(Book, book_id)
            if not b:
                return
            ef = dict(b.extra_fields or {})
            ef[key.strip()] = value
            b.extra_fields = ef
    await asyncio.to_thread(_impl)
    mark_dirty()


async def delete_book_extra_field(book_id: int, key: str) -> None:
    def _impl() -> None:
        with db_session() as s:
            b = s.get(Book, book_id)
            if not b:
                return
            ef = dict(b.extra_fields or {})
            ef.pop(key, None)
            b.extra_fields = ef
    await asyncio.to_thread(_impl)
    mark_dirty()
