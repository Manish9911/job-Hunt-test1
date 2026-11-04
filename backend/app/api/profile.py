import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.deps import get_db, get_profile
from ..db.models import Profile
from ..schemas import ProfileIn

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
    profile.location = body.location
    profile.summary = body.summary
    profile.work_authorization = body.work_authorization
    profile.sponsorship_needed = _as_bool(body.sponsorship_needed, default=profile.sponsorship_needed)
    profile.target_roles = body.target_roles
    profile.locations_json = json.dumps(_as_locations(body.locations))
    profile.min_salary = body.min_salary
    profile.linkedin = (body.linkedin or "").strip() or None
    profile.portfolio = (body.portfolio or "").strip() or None
    db.commit()
    db.refresh(profile)
    return profile_to_dict(profile)
