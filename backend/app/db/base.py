from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def make_session(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# (table, column, DDL type) — added after Phase 2. create_all only creates
# missing tables, never missing columns, so these are applied idempotently.
_EXTRA_COLUMNS = (
    ("jobs", "match_score", "INTEGER"),
    ("jobs", "match_score_tailored", "INTEGER"),
    ("jobs", "match_breakdown_json", "TEXT DEFAULT '{}'"),
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
