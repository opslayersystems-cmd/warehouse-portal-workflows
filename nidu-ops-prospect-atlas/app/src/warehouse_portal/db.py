from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from warehouse_portal.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    target = url or get_settings().database_url
    kwargs = {"connect_args": {"check_same_thread": False}} if target.startswith("sqlite") else {}
    return create_engine(target, pool_pre_ping=True, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
