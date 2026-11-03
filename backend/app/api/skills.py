from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db.deps import get_db, get_profile
from ..db.models import Profile, Skill
from ..schemas import SkillIn

router = APIRouter(prefix="/api/skills", tags=["skills"])


def _to_dict(s: Skill) -> dict:
    return {"id": s.id, "name": s.name, "category": s.category}


def _get_or_404(db: Session, profile: Profile, skill_id: int) -> Skill:
    row = db.get(Skill, skill_id)
    if row is None or row.profile_id != profile.id:
        raise HTTPException(404, "Skill not found")
    return row


@router.get("")
def list_skills(profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    rows = (
        db.query(Skill).filter(Skill.profile_id == profile.id).order_by(Skill.category, Skill.name).all()
    )
    return [_to_dict(s) for s in rows]


@router.post("", status_code=201)
def add_skill(
    body: SkillIn, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)
):
    if not body.name.strip():
        raise HTTPException(400, "Skill name is required")
    row = Skill(profile_id=profile.id, name=body.name.strip(), category=body.category.strip())
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.put("/{skill_id}")
def update_skill(
    skill_id: int,
    body: SkillIn,
    profile: Profile = Depends(get_profile),
    db: Session = Depends(get_db),
):
    row = _get_or_404(db, profile, skill_id)
    row.name = body.name.strip() or row.name
    row.category = body.category.strip()
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: int, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)
):
    row = _get_or_404(db, profile, skill_id)
    db.delete(row)
    db.commit()
    return {"deleted": skill_id}
