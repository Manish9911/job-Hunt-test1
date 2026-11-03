from typing import Generator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .models import Profile


def get_db(request: Request) -> Generator[Session, None, None]:
    session: Session = request.app.state.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_profile(db: Session = Depends(get_db)) -> Profile:
    """Return the single local profile, creating it on first use."""
    profile = db.query(Profile).first()
    if profile is None:
        profile = Profile()
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile
