from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def _configure_sqlite(dbapi_conn, connection_record):
    """WAL mode + busy timeout: the UI (readers) and the agent thread
    (writer) share one SQLite file — without WAL, an open reader transaction
    blocks the agent's writes and vice versa."""
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


def make_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite)
    return engine


def make_session(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# (table, column, DDL type) — added after Phase 2. create_all only creates
# missing tables, never missing columns, so these are applied idempotently.
_EXTRA_COLUMNS = (
    ("jobs", "match_score", "INTEGER"),
    ("jobs", "match_score_tailored", "INTEGER"),
    ("jobs", "match_breakdown_json", "TEXT DEFAULT '{}'"),
    ("jobs", "tailored_resume_id", "INTEGER"),
    ("jobs", "tailor_notes_json", "TEXT DEFAULT '{}'"),
    ("jobs", "cover_letter_text", "TEXT DEFAULT ''"),
    ("jobs", "jd_eligibility_json", "TEXT DEFAULT '{}'"),
    ("profiles", "linkedin", "VARCHAR(500)"),
    ("profiles", "portfolio", "VARCHAR(500)"),
)


def ensure_columns(engine):
    """Add any missing columns to existing SQLite tables (idempotent)."""
    if not engine.url.drivername.startswith("sqlite"):
        return
    with engine.begin() as conn:
        for table, column, col_type in _EXTRA_COLUMNS:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )
