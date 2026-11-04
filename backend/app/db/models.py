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
    # Profile links (user rule 2026-11-03: Overview form fields, auto-filled into ATS forms)
    linkedin: Mapped[str | None] = mapped_column(String(500), nullable=True)
    portfolio: Mapped[str | None] = mapped_column(String(500), nullable=True)
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


class Application(Base):
    """One application attempt (dry-run or not) for a job, using one resume version."""
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id"))
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending | dry_run_completed | captcha_manual | manual_required | failed
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    screenshot_path: Mapped[str | None] = mapped_column(String(512))
    error_text: Mapped[str | None] = mapped_column(Text)
    notes_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    job: Mapped["Job"] = relationship(back_populates="applications")
    resume: Mapped["Resume"] = relationship()
    action_log: Mapped[list["ActionLog"]] = relationship(back_populates="application")


class Notification(Base):
    """User-facing alert from the agent (CAPTCHA walls, failures, successes)."""
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    # action = needs the user now | info = FYI
    kind: Mapped[str] = mapped_column(String(20), default="info")
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text, default="")
    # external URL (e.g. the employer form) or in-app path
    link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


class ActionLog(Base):
    """Per-field audit trail for one application attempt (the 'log every action' hard rule)."""
    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    action: Mapped[str] = mapped_column(String(60))
    field: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    application: Mapped["Application"] = relationship(back_populates="action_log")


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    website: Mapped[str] = mapped_column(String(500), default="")
    locations_json: Mapped[str] = mapped_column(Text, default="[]")
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    region: Mapped[str] = mapped_column(String(10), default="other")  # us | eu | other
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ATS discovery (Phase 2)
    ats: Mapped[str] = mapped_column(String(30), default="unknown")
    careers_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    jobs_endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ats_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(400), index=True)
    company: Mapped[str] = mapped_column(String(300), default="")
    company_website: Mapped[str] = mapped_column(String(500), default="")
    location: Mapped[str] = mapped_column(String(400), default="")
    countries_json: Mapped[str] = mapped_column(Text, default="[]")
    remote: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    visa_sponsorship: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    commitment_json: Mapped[str] = mapped_column(Text, default="[]")
    yoe: Mapped[float | None] = mapped_column(nullable=True)
    salary_min: Mapped[int | None] = mapped_column(nullable=True)
    salary_max: Mapped[int | None] = mapped_column(nullable=True)
    salary_currency: Mapped[str] = mapped_column(String(10), default="USD")
    apply_url: Mapped[str] = mapped_column(String(1000), default="")
    source: Mapped[str] = mapped_column(String(60), default="")  # hiringcafe|greenhouse|lever|...
    hiringcafe_id: Mapped[str] = mapped_column(String(120), default="")
    indexed_at: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Filtering result at ingest time (informational; re-evaluable anytime)
    passes_filters: Mapped[bool] = mapped_column(Boolean, default=True)
    filter_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    # Resume-vs-JD match, 0-100. Master resume = "before tailoring";
    # tailored resume = "after tailoring" (filled by Phase 3).
    match_score: Mapped[int | None] = mapped_column(nullable=True)
    match_score_tailored: Mapped[int | None] = mapped_column(nullable=True)
    match_breakdown_json: Mapped[str] = mapped_column(Text, default="{}")
    # Phase 3 tailoring artifacts
    tailored_resume_id: Mapped[int | None] = mapped_column(nullable=True)
    tailor_notes_json: Mapped[str] = mapped_column(Text, default="{}")
    cover_letter_text: Mapped[str] = mapped_column(Text, default="")
    # Phase 5: JD eligibility gate result ({"decision": "apply"|"reject", ...})
    jd_eligibility_json: Mapped[str] = mapped_column(Text, default="{}")
    # Deduplication key: sha1(source|apply_url|company+title)
    unique_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    applications: Mapped[list["Application"]] = relationship(back_populates="job")
