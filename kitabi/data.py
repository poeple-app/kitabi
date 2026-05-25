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


class Book(Base):
    """A book the user is reading or has read."""

    __tablename__ = "books"

    id = Column(Integer, primary_key=True)
    short_code = Column(String(8), unique=True, index=True, nullable=False)
    title = Column(String(255), nullable=False, index=True)
    author = Column(String(255))
    isbn = Column(String(32), index=True)
    genre = Column(String(100))
    subgenre = Column(String(100))
    total_pages = Column(Integer)
    read_pages = Column(Integer, default=0, nullable=False)
    status = Column(SAEnum(BookStatus), default=BookStatus.NOT_STARTED, nullable=False)
    cover_url = Column(String(500))
    bought_from = Column(String(255))
    price_tl = Column(Integer)
    bought_at = Column(DateTime)
    tags = Column(JSON, default=list)
    personal_note = Column(Text)
    goodreads_url = Column(String(500))
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
    _setup_fts5()
    _ensure_singleton_settings()
    logger.info("data.init_db.success", db_path=db_path)


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
) -> Note:
    """Insert a new note. Allocates `code` like 'SUC001' from the book counter."""
    logger.info(
        "data.add_note.start",
        book_id=book_id,
        session_id=session_id,
        category=category.value,
        page=page,
        from_qa=from_qa,
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
    """All WORD + CONCEPT notes, alphabetical by transcript."""
    def _impl() -> list[tuple[Note, Book]]:
        with db_session() as s:
            q = (
                select(Note)
                .where(Note.category.in_([Category.WORD, Category.CONCEPT]))
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


def _compute_book_stats(
    book: Book, sessions: list[Session], notes: list[Note]
) -> dict[str, Any]:
    """Aggregate stats shown on the PDF stats page."""
    total_min = sum((sess.duration_min or 0) for sess in sessions)
    category_counts: dict[str, int] = {cat.value: 0 for cat in Category}
    for note in notes:
        category_counts[note.category.value] += 1
    return {
        "session_count": len(sessions),
        "total_minutes": total_min,
        "hours": total_min // 60,
        "minutes_remainder": total_min % 60,
        "note_count": len(notes),
        "category_counts": category_counts,
        "started_at": to_local(sessions[0].started_at) if sessions else None,
        "ended_at": (
            to_local(sessions[-1].ended_at) if sessions and sessions[-1].ended_at else None
        ),
    }


async def render_pdf(book_id: int) -> bytes:
    """Generate the reading-journal PDF for a book."""
    t0 = time.time()
    logger.info("data.render_pdf.start", book_id=book_id)

    def _impl() -> bytes:
        with db_session() as s:
            book = s.get(Book, book_id)
            if not book:
                raise ValueError(f"Book {book_id} not found")
            sessions = sorted(book.sessions, key=lambda x: x.started_at)
            notes = sorted(book.notes, key=lambda x: x.created_at)
            favorites = [n for n in notes if n.is_favorite]

            template = _jinja_env.get_template("journal.html")
            html_str = template.render(
                book=book,
                sessions=sessions,
                notes=notes,
                Category=Category,
                glossary=[
                    n for n in notes if n.category in (Category.WORD, Category.CONCEPT)
                ],
                summaries=[n for n in notes if n.category == Category.SUMMARY],
                favorites=favorites,
                stats=_compute_book_stats(book, sessions, notes),
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


# ─────────────────────────── Book metadata lookup (Google Books) ───────────────────────────


GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"


async def lookup_book_metadata(isbn: str) -> dict[str, Any] | None:
    """Fetch book metadata from Google Books by ISBN."""
    isbn_clean = "".join(c for c in isbn if c.isdigit() or c == "X")
    if not isbn_clean:
        logger.warning("data.lookup_book.invalid_isbn", isbn=isbn)
        return None

    logger.info("data.lookup_book.start", isbn=isbn_clean)
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
            "data.lookup_book.http_failed",
            isbn=isbn_clean,
            error=str(e),
            duration_ms=int((time.time() - t0) * 1000),
        )
        return None

    items = payload.get("items") or []
    if not items:
        logger.info("data.lookup_book.not_found", isbn=isbn_clean)
        return None

    info = items[0].get("volumeInfo", {})
    authors = info.get("authors") or []
    image_links = info.get("imageLinks") or {}
    categories = info.get("categories") or []

    cover_url = (
        image_links.get("extraLarge")
        or image_links.get("large")
        or image_links.get("medium")
        or image_links.get("thumbnail")
        or image_links.get("smallThumbnail")
    )
    if cover_url and cover_url.startswith("http://"):
        cover_url = "https://" + cover_url[len("http://"):]

    return {
        "title": info.get("title") or "",
        "author": ", ".join(authors) if authors else None,
        "genre": categories[0] if categories else None,
        "subgenre": categories[1] if len(categories) > 1 else None,
        "total_pages": info.get("pageCount"),
        "cover_url": cover_url,
        "isbn": isbn_clean,
        "goodreads_url": None,
    }


# ─────────────────────────── Serialization helpers ───────────────────────────


def _book_to_dict(b: Book) -> dict[str, Any]:
    return {
        "id": b.id,
        "short_code": b.short_code,
        "title": b.title,
        "author": b.author,
        "isbn": b.isbn,
        "genre": b.genre,
        "subgenre": b.subgenre,
        "total_pages": b.total_pages,
        "read_pages": b.read_pages,
        "status": b.status.value if b.status else None,
        "cover_url": b.cover_url,
        "bought_from": b.bought_from,
        "price_tl": b.price_tl,
        "bought_at": b.bought_at.isoformat() if b.bought_at else None,
        "tags": b.tags,
        "personal_note": b.personal_note,
        "rating": b.rating,
        "one_line_review": b.one_line_review,
        "would_recommend": b.would_recommend,
        "goodreads_url": b.goodreads_url,
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
    }
