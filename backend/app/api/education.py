from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db.deps import get_db, get_profile
from ..db.models import Education, Profile
from ..schemas import EducationIn

router = APIRouter(prefix="/api/education", tags=["education"])


def _to_dict(e: Education) -> dict:
    return {
        "id": e.id,
        "school": e.school,
        "degree": e.degree,
        "field": e.field,
        "start_date": e.start_date,
        "end_date": e.end_date,
    }


def _get_or_404(db: Session, profile: Profile, row_id: int) -> Education:
    row = db.get(Education, row_id)
    if row is None or row.profile_id != profile.id:
        raise HTTPException(404, "Education entry not found")
    return row


@router.get("")
def list_education(profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    rows = (
        db.query(Education).filter(Education.profile_id == profile.id).order_by(Education.id).all()
    )
    return [_to_dict(e) for e in rows]


@router.post("", status_code=201)
def add_education(
    body: EducationIn, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)
):
    if not body.school.strip():
        raise HTTPException(400, "School is required")
    row = Education(
        profile_id=profile.id,
        school=body.school.strip(),
        degree=body.degree.strip(),
        field=body.field.strip(),
        start_date=body.start_date.strip(),
        end_date=body.end_date.strip(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.put("/{row_id}")
def update_education(
    row_id: int,
    body: EducationIn,
    profile: Profile = Depends(get_profile),
    db: Session = Depends(get_db),
):
    row = _get_or_404(db, profile, row_id)
    row.school = body.school.strip() or row.school
    row.degree = body.degree.strip()
    row.field = body.field.strip()
    row.start_date = body.start_date.strip()
    row.end_date = body.end_date.strip()
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.delete("/{row_id}")
def delete_education(
    row_id: int, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)
):
    row = _get_or_404(db, profile, row_id)
    db.delete(row)
    db.commit()
    return {"deleted": row_id}
