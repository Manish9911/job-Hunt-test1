from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .db.deps import get_db, get_profile
from .db.models import CommonAnswer, Education, Profile, Resume, Skill, WorkHistory

router = APIRouter(tags=["pages"])


def _ctx(active: str, **extra) -> dict:
    ctx = {"active": active, "title": "jobflow"}
    ctx.update(extra)
    return ctx


def _render(request: Request, template: str, **ctx):
    return request.app.state.templates.TemplateResponse(request, template, ctx)


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    profile: Profile = Depends(get_profile),
    db: Session = Depends(get_db),
):
    master = (
        db.query(Resume)
        .filter(Resume.profile_id == profile.id, Resume.is_master.is_(True))
        .first()
    )
    pid = profile.id
    counts = {
        "resumes": db.query(Resume).filter(Resume.profile_id == pid).count(),
        "skills": db.query(Skill).filter(Skill.profile_id == pid).count(),
        "work_history": db.query(WorkHistory).filter(WorkHistory.profile_id == pid).count(),
        "education": db.query(Education).filter(Education.profile_id == pid).count(),
        "common_answers": db.query(CommonAnswer).filter(CommonAnswer.profile_id == pid).count(),
    }
    return _render(request, "index.html", **_ctx("overview", profile=profile, master=master, counts=counts))


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, profile: Profile = Depends(get_profile)):
    import json

    try:
        locations = json.loads(profile.locations_json) if profile.locations_json else []
    except json.JSONDecodeError:
        locations = []
    return _render(
        request,
        "profile.html",
        **_ctx("profile", profile=profile, locations=locations),
    )


@router.get("/resumes", response_class=HTMLResponse)
def resumes_page(request: Request, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    items = (
        db.query(Resume).filter(Resume.profile_id == profile.id).order_by(Resume.id).all()
    )
    return _render(request, "resumes.html", **_ctx("resumes", resumes=items))


@router.get("/skills", response_class=HTMLResponse)
def skills_page(request: Request, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    items = (
        db.query(Skill)
        .filter(Skill.profile_id == profile.id)
        .order_by(Skill.category, Skill.name)
        .all()
    )
    return _render(request, "skills.html", **_ctx("skills", skills=items))


@router.get("/experience", response_class=HTMLResponse)
def experience_page(request: Request, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    items = (
        db.query(WorkHistory).filter(WorkHistory.profile_id == profile.id).order_by(WorkHistory.id).all()
    )
    return _render(request, "experience.html", **_ctx("experience", history=items))


@router.get("/education", response_class=HTMLResponse)
def education_page(request: Request, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    items = (
        db.query(Education).filter(Education.profile_id == profile.id).order_by(Education.id).all()
    )
    return _render(request, "education.html", **_ctx("education", education=items))


@router.get("/answers", response_class=HTMLResponse)
def answers_page(request: Request, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    items = (
        db.query(CommonAnswer)
        .filter(CommonAnswer.profile_id == profile.id)
        .order_by(CommonAnswer.category, CommonAnswer.id)
        .all()
    )
    return _render(request, "answers.html", **_ctx("answers", answers=items))