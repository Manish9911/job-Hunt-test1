from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(320), default="")
    phone: Mapped[str | None] = mapped_column(String(60), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Work authorization (used by Phase 4 screening-question answers)
    work_authorization: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sponsorship_needed: Mapped[bool] = mapped_column(Boolean, default=True)
    # Job-search preferences
    target_roles: Mapped[str | None] = mapped_column(Text, nullable=True)
    locations_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    min_salary: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    resumes: Mapped[list["Resume"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    skills: Mapped[list["Skill"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    work_history: Mapped[list["WorkHistory"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    education: Mapped[list["Education"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    common_answers: Mapped[list["CommonAnswer"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), index=True)
    label: Mapped[str] = mapped_column(String(200), default="")
    original_filename: Mapped[str] = mapped_column(String(500))
    storage_path: Mapped[str] = mapped_column(String(500))
    content_json: Mapped[str] = mapped_column(Text, default="{}")
    is_master: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    profile: Mapped["Profile"] = relationship(back_populates="resumes")


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(100), default="")

    profile: Mapped["Profile"] = relationship(back_populates="skills")


class WorkHistory(Base):
    __tablename__ = "work_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), index=True)
    company: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(300), default="")
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    start_date: Mapped[str] = mapped_column(String(40), default="")
    end_date: Mapped[str] = mapped_column(String(40), default="")
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    bullets_json: Mapped[str] = mapped_column(Text, default="[]")

    profile: Mapped["Profile"] = relationship(back_populates="work_history")


class Education(Base):
    __tablename__ = "education"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), index=True)
    school: Mapped[str] = mapped_column(String(300))
    degree: Mapped[str] = mapped_column(String(300), default="")
    field: Mapped[str] = mapped_column(String(300), default="")
    start_date: Mapped[str] = mapped_column(String(40), default="")
    end_date: Mapped[str] = mapped_column(String(40), default="")

    profile: Mapped["Profile"] = relationship(back_populates="education")


class CommonAnswer(Base):
    __tablename__ = "common_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), index=True)
    category: Mapped[str] = mapped_column(String(100), default="")
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)

    profile: Mapped["Profile"] = relationship(back_populates="common_answers")
