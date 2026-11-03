import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db.deps import get_db
from ..db.models import Company, Job
from ..ingestion import service

router = APIRouter(prefix="/api/ingest", tags=["ingestion"])
router_public = APIRouter(prefix="/api", tags=["jobs"])


def _company_dict(c: Company) -> dict:
    try:
        locations = json.loads(c.locations_json) if c.locations_json else []
    except json.JSONDecodeError:
        locations = []
    return {
        "id": c.id,
        "name": c.name,
        "website": c.website,
        "locations": locations,
        "remote": c.remote,
        "region": c.region,
        "ats": c.ats,
        "careers_url": c.careers_url,
        "jobs_endpoint": bool(c.jobs_endpoint),
        "ats_checked_at": c.ats_checked_at.isoformat() if c.ats_checked_at else None,
    }


def _job_dict(j: Job) -> dict:
    try:
        countries = json.loads(j.countries_json) if j.countries_json else []
    except json.JSONDecodeError:
        countries = []
    try:
        reasons = json.loads(j.filter_reasons_json) if j.filter_reasons_json else []
    except json.JSONDecodeError:
        reasons = []
    try:
        breakdown = json.loads(j.match_breakdown_json) if j.match_breakdown_json else {}
    except json.JSONDecodeError:
        breakdown = {}
    lift = None
    if j.match_score is not None and j.match_score_tailored is not None:
        lift = j.match_score_tailored - j.match_score
    return {
        "id": j.id,
        "title": j.title,
        "company": j.company,
        "company_website": j.company_website,
        "location": j.location,
        "countries": countries,
        "remote": j.remote,
        "visa_sponsorship": j.visa_sponsorship,
        "yoe": j.yoe,
        "salary_min": j.salary_min,
        "salary_max": j.salary_max,
        "salary_currency": j.salary_currency,
        "apply_url": j.apply_url,
        "source": j.source,
        "passes_filters": j.passes_filters,
        "filter_reasons": reasons,
        "match_score": j.match_score,
        "match_score_tailored": j.match_score_tailored,
        "match_lift": lift,
        "match_breakdown": breakdown,
        "fetched_at": j.fetched_at.isoformat() if j.fetched_at else None,
    }


# ---------- trigger ingestion ----------

@router.post("/companies")
def run_ingest_companies(db: Session = Depends(get_db)):
    return service.ingest_hwow(db)


@router.post("/ats")
def run_ingest_ats(
    limit: int = Query(25, ge=1, le=100),
    recheck: bool = Query(False),
    db: Session = Depends(get_db),
):
    return service.discover_ats(db, limit=limit, recheck=recheck)


class ManualHtmlIn(BaseModel):
    html: str


@router.post("/jobs/hiring-cafe")
def run_ingest_hiring_cafe(db: Session = Depends(get_db)):
    return service.ingest_hiring_cafe(db)


@router.post("/jobs/abstractvc")
def run_ingest_abstractvc(db: Session = Depends(get_db)):
    return service.ingest_abstractvc(db)


@router.post("/jobs/hiring-cafe/manual")
def run_ingest_hiring_cafe_manual(body: ManualHtmlIn, db: Session = Depends(get_db)):
    if len(body.html) < 500:
        raise HTTPException(400, "HTML too small to be a saved hiring.cafe page")
    return service.ingest_hiring_cafe_manual(db, body.html)


@router.post("/jobs/companies")
def run_ingest_company_feeds(limit: int = Query(25, ge=1, le=100), db: Session = Depends(get_db)):
    return service.ingest_company_feed_jobs(db, limit=limit)


@router.post("/jobs/refilter")
def run_refilter_jobs(db: Session = Depends(get_db)):
    return service.refilter_jobs(db)


@router.post("/jobs/score")
def run_score_jobs(db: Session = Depends(get_db)):
    return service.score_all_jobs(db)


# ---------- read companies ----------

@router_public.get("/companies")
def list_companies(
    region: str | None = Query(None),
    ats: str | None = Query(None),
    q: str | None = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(Company)
    if region:
        query = query.filter(Company.region == region)
    if ats:
        query = query.filter(Company.ats == ats)
    if q:
        query = query.filter(Company.name.ilike(f"%{q}%"))
    rows = query.order_by(Company.name).all()
    return [_company_dict(c) for c in rows]


# ---------- read jobs ----------

@router_public.get("/jobs")
def list_jobs(
    passing: bool | None = Query(None),
    source: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(Job)
    if passing is not None:
        query = query.filter(Job.passes_filters.is_(passing))
    if source:
        query = query.filter(Job.source == source)
    if q:
        like = f"%{q}%"
        query = query.filter((Job.title.ilike(like)) | (Job.company.ilike(like)))
    rows = query.order_by(Job.fetched_at.desc(), Job.id.desc()).limit(limit).all()
    return [_job_dict(j) for j in rows]


@router_public.get("/jobs/count")
def jobs_count(db: Session = Depends(get_db)):
    total = db.query(Job).count()
    passing = db.query(Job).filter(Job.passes_filters.is_(True)).count()
    return {"total": total, "passing": passing}
