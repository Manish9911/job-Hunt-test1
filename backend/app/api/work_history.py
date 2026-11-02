import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db.deps import get_db, get_profile
from ..db.models import Profile, WorkHistory
from ..schemas import WorkHistoryIn

router = APIRouter(prefix="/api/work-history", tags=["work-history"])


def _as_bool(value: bool | str, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def _as_bullets(value: list[str] | str) -> list[str]:
    if isinstance(value, str):
        return [ln.strip() for ln in value.splitlines() if ln.strip()]
    return [ln.strip() for ln in value if ln.strip()]


def _to_dict(w: WorkHistory) -> dict:
    try:
        bullets = json.loads(w.bullets_json) if w.bullets_json else []
    except json.JSONDecodeError:
        bullets = []
    return {
        "id": w.id,
        "company": w.company,
        "title": w.title,
        "location": w.location,
        "start_date": w.start_date,
        "end_date": w.end_date,
        "is_current": w.is_current,
        "bullets": bullets,
    }


def _get_or_404(db: Session, profile: Profile, row_id: int) -> WorkHistory:
    row = db.get(WorkHistory, row_id)
    if row is None or row.profile_id != profile.id:
        raise HTTPException(404, "Work history entry not found")
    return row


@router.get("")
def list_history(profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    rows = (
        db.query(WorkHistory).filter(WorkHistory.profile_id == profile.id).order_by(WorkHistory.id).all()
    )
    return [_to_dict(w) for w in rows]


@router.post("", status_code=201)
def add_history(
    body: WorkHistoryIn, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)
):
    if not body.company.strip():
        raise HTTPException(400, "Company is required")
    row = WorkHistory(
        profile_id=profile.id,
        company=body.company.strip(),
        title=body.title.strip(),
        location=body.location.strip() if body.location else None,
        start_date=body.start_date.strip(),
        end_date=body.end_date.strip(),
        is_current=_as_bool(body.is_current),
        bullets_json=json.dumps(_as_bullets(body.bullets)),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.put("/{row_id}")
def update_history(
    row_id: int,
    body: WorkHistoryIn,
    profile: Profile = Depends(get_profile),
    db: Session = Depends(get_db),
):
    row = _get_or_404(db, profile, row_id)
    row.company = body.company.strip() or row.company
    row.title = body.title.strip()
    row.location = body.location.strip() if body.location else None
    row.start_date = body.start_date.strip()
    row.end_date = body.end_date.strip()
    row.is_current = _as_bool(body.is_current)
    row.bullets_json = json.dumps(_as_bullets(body.bullets))
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.delete("/{row_id}")
def delete_history(
    row_id: int, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)
):
    row = _get_or_404(db, profile, row_id)
    db.delete(row)
    db.commit()
    return {"deleted": row_id}
