import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db.deps import get_db, get_profile
from ..db.models import Profile
from ..schemas import ProfileIn, ProfileLinksIn, ProfilePermissionsIn

router = APIRouter(prefix="/api/profile", tags=["profile"])


def _as_bool(value: bool | str, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "false", "0", "no")


def _as_locations(value: list[str] | str) -> list[str]:
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return [s.strip() for s in value if s.strip()]


def profile_to_dict(p: Profile) -> dict:
    try:
        locations = json.loads(p.locations_json) if p.locations_json else []
    except json.JSONDecodeError:
        locations = []
    return {
        "id": p.id,
        "full_name": p.full_name,
        "email": p.email,
        "phone": p.phone,
        "location": p.location,
        "summary": p.summary,
        "work_authorization": p.work_authorization,
        "sponsorship_needed": p.sponsorship_needed,
        "target_roles": p.target_roles,
        "locations": locations,
        "min_salary": p.min_salary,
        "linkedin": p.linkedin,
        "portfolio": p.portfolio,
        "submission_mode": p.submission_mode,
        "outreach_permitted": bool(p.outreach_permitted),
    }


@router.get("")
def read_profile(profile: Profile = Depends(get_profile)):
    return profile_to_dict(profile)


@router.put("")
def update_profile(
    body: ProfileIn,
    profile: Profile = Depends(get_profile),
    db: Session = Depends(get_db),
):
    profile.full_name = body.full_name
    profile.email = body.email
    profile.phone = body.phone
    profile.summary = body.summary
    profile.work_authorization = body.work_authorization
    profile.sponsorship_needed = _as_bool(body.sponsorship_needed, default=profile.sponsorship_needed)
    profile.target_roles = body.target_roles
    profile.locations_json = json.dumps(_as_locations(body.locations))
    profile.min_salary = body.min_salary
    # linkedin / portfolio / location live in the dedicated "Links & location"
    # column (PATCH /api/profile/links) — the main form must never clobber them.
    db.commit()
    db.refresh(profile)
    return profile_to_dict(profile)


@router.patch("/links")
def update_profile_links(
    body: ProfileLinksIn,
    profile: Profile = Depends(get_profile),
    db: Session = Depends(get_db),
):
    """The dedicated Overview column (user rule 2026-11-04): LinkedIn,
    Portfolio/GitHub and the current location — the single set of values the
    agent always fills into ATS forms. Only these three fields are touched,
    so saving the main profile form can never wipe them (or vice versa)."""
    profile.linkedin = (body.linkedin or "").strip() or None
    profile.portfolio = (body.portfolio or "").strip() or None
    profile.location = (body.location or "").strip() or None
    db.commit()
    db.refresh(profile)
    return profile_to_dict(profile)


@router.patch("/permissions")
def update_profile_permissions(
    body: ProfilePermissionsIn,
    profile: Profile = Depends(get_profile),
    db: Session = Depends(get_db),
):
    """The agent-permission card (user rule 2026-11-09). Only the two
    permission fields are touched, so the main profile form can never reset
    them (or vice versa)."""
    if body.submission_mode not in ("autonomous", "approve_each"):
        raise HTTPException(400, "submission_mode must be 'autonomous' or 'approve_each'")
    profile.submission_mode = body.submission_mode
    profile.outreach_permitted = bool(body.outreach_permitted)
    db.commit()
    db.refresh(profile)
    return profile_to_dict(profile)
