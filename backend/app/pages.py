from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .db.deps import get_db, get_profile
from .db.models import (
    ActionLog,
    Application,
    CommonAnswer,
    Company,
    Education,
    Job,
    Profile,
    Resume,
    Skill,
    WorkHistory,
)

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
    """Consolidated Overview: profile, resumes, skills, experience, education,
    answers and companies all live on this single page (no separate tabs)."""
    import json

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
        "companies": db.query(Company).count(),
    }
    try:
        locations = json.loads(profile.locations_json) if profile.locations_json else []
    except json.JSONDecodeError:
        locations = []
    resumes = db.query(Resume).filter(Resume.profile_id == pid).order_by(Resume.id).all()
    skills = (
        db.query(Skill).filter(Skill.profile_id == pid)
        .order_by(Skill.category, Skill.name).all()
    )
    history = db.query(WorkHistory).filter(WorkHistory.profile_id == pid).order_by(WorkHistory.id).all()
    education = db.query(Education).filter(Education.profile_id == pid).order_by(Education.id).all()
    answers = (
        db.query(CommonAnswer).filter(CommonAnswer.profile_id == pid)
        .order_by(CommonAnswer.category, CommonAnswer.id).all()
    )
    companies = db.query(Company).order_by(Company.name).all()
    by_region: dict[str, int] = {}
    by_ats: dict[str, int] = {}
    for c in companies:
        by_region[c.region] = by_region.get(c.region, 0) + 1
        by_ats[c.ats] = by_ats.get(c.ats, 0) + 1
    return _render(
        request, "index.html",
        **_ctx(
            "overview",
            profile=profile, master=master, counts=counts, locations=locations,
            resumes=resumes, skills=skills, history=history, education=education,
            answers=answers, companies=companies,
            by_region=by_region, by_ats=by_ats,
        ),
    )


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


@router.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request, db: Session = Depends(get_db)):
    companies = db.query(Company).order_by(Company.name).all()
    by_region: dict[str, int] = {}
    by_ats: dict[str, int] = {}
    for c in companies:
        by_region[c.region] = by_region.get(c.region, 0) + 1
        by_ats[c.ats] = by_ats.get(c.ats, 0) + 1
    return _render(
        request, "companies.html",
        **_ctx("companies", companies=companies, by_region=by_region, by_ats=by_ats),
    )


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    all: bool = Query(False, description="show non-matching jobs too"),
    db: Session = Depends(get_db),
):
    total = db.query(Job).count()
    passing = db.query(Job).filter(Job.passes_filters.is_(True)).count()
    query = db.query(Job)
    if not all:
        query = query.filter(Job.passes_filters.is_(True))
    jobs = (
        query.order_by(Job.match_score.desc().nulls_last(), Job.id.desc())
        .limit(200)
        .all()
    )
    return _render(
        request, "jobs.html",
        **_ctx("jobs", jobs=jobs, total=total, passing=passing, show_all=all),
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail_page(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        from fastapi.responses import HTMLResponse as _HTML

        return _HTML("<h1>Job not found</h1>", status_code=404)
    from .api.tailor import tailored_view

    return _render(request, "job_detail.html", **_ctx("jobs", job=job, tailored=tailored_view(db, job)))


@router.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request, db: Session = Depends(get_db)):
    rows = (
        db.query(Application)
        .order_by(Application.created_at.desc(), Application.id.desc())
        .limit(200)
        .all()
    )
    app_list = []
    for a in rows:
        log_count = (
            db.query(ActionLog)
            .filter(ActionLog.application_id == a.id)
            .count()
        )
        app_list.append(
            {
                "id": a.id,
                "status": a.status,
                "dry_run": a.dry_run,
                "error": a.error_text,
                "created_at": a.created_at,
                "log_count": log_count,
                "has_screenshot": bool(a.screenshot_path),
                "screenshot_url": f"/api/applications/{a.id}/screenshot" if a.screenshot_path else None,
                "job": a.job,
                "resume": a.resume,
            }
        )
    return _render(request, "applications.html", **_ctx("applications", apps=app_list))