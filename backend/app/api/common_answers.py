from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db.deps import get_db, get_profile
from ..db.models import CommonAnswer, Profile
from ..schemas import CommonAnswerIn

router = APIRouter(prefix="/api/common-answers", tags=["common-answers"])


def _to_dict(a: CommonAnswer) -> dict:
    return {"id": a.id, "category": a.category, "question": a.question, "answer": a.answer}


def _get_or_404(db: Session, profile: Profile, row_id: int) -> CommonAnswer:
    row = db.get(CommonAnswer, row_id)
    if row is None or row.profile_id != profile.id:
        raise HTTPException(404, "Answer not found")
    return row


@router.get("")
def list_answers(profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    rows = (
        db.query(CommonAnswer)
        .filter(CommonAnswer.profile_id == profile.id)
        .order_by(CommonAnswer.category, CommonAnswer.id)
        .all()
    )
    return [_to_dict(a) for a in rows]


@router.post("", status_code=201)
def add_answer(
    body: CommonAnswerIn, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)
):
    if not body.question.strip():
        raise HTTPException(400, "Question is required")
    row = CommonAnswer(
        profile_id=profile.id,
        category=body.category.strip(),
        question=body.question.strip(),
        answer=body.answer.strip(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.put("/{row_id}")
def update_answer(
    row_id: int,
    body: CommonAnswerIn,
    profile: Profile = Depends(get_profile),
    db: Session = Depends(get_db),
):
    row = _get_or_404(db, profile, row_id)
    row.category = body.category.strip()
    row.question = body.question.strip() or row.question
    row.answer = body.answer.strip()
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.delete("/{row_id}")
def delete_answer(
    row_id: int, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)
):
    row = _get_or_404(db, profile, row_id)
    db.delete(row)
    db.commit()
    return {"deleted": row_id}
