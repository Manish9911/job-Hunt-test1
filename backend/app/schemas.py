from pydantic import BaseModel


class ProfileIn(BaseModel):
    full_name: str = ""
    email: str = ""
    phone: str | None = None
    location: str | None = None
    summary: str | None = None
    work_authorization: str | None = None
    # bool in JSON bodies, "true"/absent from HTML form checkboxes
    sponsorship_needed: bool | str = True
    target_roles: str | None = None
    # list in JSON bodies, comma-separated string from forms
    locations: list[str] | str = ""
    min_salary: str | None = None
    # linkedin / portfolio are edited in the dedicated Overview "Links &
    # location" card (PATCH /api/profile/links) — kept here for API compat.
    linkedin: str | None = None
    portfolio: str | None = None


class ProfileLinksIn(BaseModel):
    """Dedicated Overview column (user rule 2026-11-04): LinkedIn,
    Portfolio/GitHub and the current location — the values the agent always
    fills into ATS forms. Kept together in one card so the user updates them
    in one obvious place."""
    linkedin: str | None = None
    portfolio: str | None = None
    location: str | None = None


class SkillIn(BaseModel):
    name: str
    category: str = ""


class WorkHistoryIn(BaseModel):
    company: str
    title: str = ""
    location: str | None = None
    start_date: str = ""
    end_date: str = ""
    is_current: bool | str = False
    # list in JSON bodies, newline-separated string from forms
    bullets: list[str] | str = []


class EducationIn(BaseModel):
    school: str
    degree: str = ""
    field: str = ""
    start_date: str = ""
    end_date: str = ""


class CommonAnswerIn(BaseModel):
    category: str = ""
    question: str
    answer: str
