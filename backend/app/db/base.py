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
