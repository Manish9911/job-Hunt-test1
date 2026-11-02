"""Ingestion orchestration: fetch -> normalize -> filter -> dedupe -> store."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from ..db.models import Company, Job, Skill
from .ats import detect_company_ats, registrable_domain
from .ats_jobs import fetch_jobs_for_endpoint
from .abstractvc import (
    fetch_all_jobs,
    make_client as make_abstractvc_client,
    parse_abstractvc_response,
    bootstrap_token,
)
from .filters import job_passes_filters
from .hiring_cafe import (
    DEFAULT_PAGES,
    IngestionBlocked,
    fetch_page,
    make_client,
    parse_hiring_cafe_html,
)
from .hwow import domain_of, fetch_hwow_readme, parse_hwow_readme
from .match import score_job


def load_resume_skills(db: Session) -> list[str]:
    """All profile skills — the master resume's skill inventory."""
    return [r[0] for r in db.query(Skill.name).all()]


def _score_row(row: Job, job_dict: dict, resume_skills: list[str]) -> None:
    if not resume_skills:
        return
    score, breakdown = score_job(job_dict, resume_skills)
    row.match_score = score
    row.match_breakdown_json = json.dumps(breakdown)


def job_unique_key(job: dict) -> str:
    core = job.get("apply_url") or f"{job.get('company', '')}|{job.get('title', '')}"
    return hashlib.sha1(f"{job.get('source', '')}|{core}".encode("utf-8")).hexdigest()


def upsert_companies(db: Session, raw_companies: list[dict]) -> dict:
    added = updated = 0
    for rc in raw_companies:
        row = db.query(Company).filter(Company.name == rc["name"]).first()
        if row is None:
            row = Company(name=rc["name"])
            db.add(row)
            added += 1
        else:
            updated += 1
        row.website = rc.get("website") or row.website
        row.locations_json = json.dumps(rc.get("locations") or [])
        row.remote = bool(rc.get("remote"))
        row.region = rc.get("region") or "other"
        row.notes = rc.get("notes") or row.notes
    db.commit()
    return {"total": len(raw_companies), "added": added, "updated": updated}


def store_jobs(db: Session, jobs: list[dict]) -> dict:
    added = skipped = passing = 0
    resume_skills = load_resume_skills(db)
    for j in jobs:
        key = job_unique_key(j)
        if db.query(Job).filter(Job.unique_key == key).first() is not None:
            skipped += 1
            continue
        keep, reasons = job_passes_filters(j)
        row = Job(
            title=j.get("title") or "",
            company=j.get("company") or "",
            company_website=j.get("company_website") or "",
            location=j.get("location") or "",
            countries_json=json.dumps(j.get("countries") or []),
            remote=j.get("remote"),
            visa_sponsorship=j.get("visa_sponsorship"),
            commitment_json=json.dumps(j.get("commitment") or []),
            yoe=j.get("yoe"),
            salary_min=j.get("salary_min"),
            salary_max=j.get("salary_max"),
            salary_currency=j.get("salary_currency") or "USD",
            apply_url=j.get("apply_url") or "",
            source=j.get("source") or "",
            hiringcafe_id=j.get("hiringcafe_id") or "",
            indexed_at=j.get("indexed_at"),
            passes_filters=keep,
            filter_reasons_json=json.dumps(reasons),
            unique_key=key,
        )
        _score_row(row, j, resume_skills)
        db.add(row)
        added += 1
        if keep:
            passing += 1
    db.commit()
    return {"seen": len(jobs), "added": added, "duplicates_skipped": skipped, "passing_filters": passing}


def ingest_hwow(db: Session) -> dict:
    text = fetch_hwow_readme()
    return upsert_companies(db, parse_hwow_readme(text))


def ingest_hwow_from_text(db: Session, text: str) -> dict:
    return upsert_companies(db, parse_hwow_readme(text))


def discover_ats(db: Session, limit: int = 25, recheck: bool = False, delay: float = 1.2) -> dict:
    q = db.query(Company)
    if not recheck:
        q = q.filter(Company.ats == "unknown")
    targets = q.order_by(Company.id).limit(limit).all()
    results = {"checked": 0, "by_ats": {}, "errors": []}
    with make_client() as client:
        for comp in targets:
            domain = domain_of(comp.website) if comp.website else ""
            if not domain:
                results["errors"].append(f"{comp.name}: no website")
                continue
            try:
                found = detect_company_ats(domain, client)
                comp.ats = found["ats"]
                comp.careers_url = found["careers_url"]
                comp.jobs_endpoint = found["jobs_endpoint"]
                comp.ats_checked_at = datetime.now(timezone.utc)
                results["checked"] += 1
                results["by_ats"][found["ats"]] = results["by_ats"].get(found["ats"], 0) + 1
                db.commit()
            except httpx.HTTPError as exc:
                results["errors"].append(f"{comp.name}: {exc.__class__.__name__}")
            time.sleep(delay)
    return results


def ingest_hiring_cafe(db: Session, pages: list[str] | None = None) -> dict:
    results = {"pages": 0, "errors": []}
    all_jobs: list[dict] = []
    with make_client() as client:
        for path in (pages or DEFAULT_PAGES):
            try:
                html = fetch_page(client, path)
                jobs = parse_hiring_cafe_html(html)
                results["pages"] += 1
                all_jobs.extend(jobs)
            except IngestionBlocked as exc:
                results["errors"].append(f"{path}: BLOCKED — {exc}")
            except (ValueError, httpx.HTTPError) as exc:
                results["errors"].append(f"{path}: {exc.__class__.__name__}: {str(exc)[:120]}")
            time.sleep(2.0)
    results["stored"] = store_jobs(db, all_jobs)
    return results


def ingest_hiring_cafe_manual(db: Session, html: str) -> dict:
    jobs = parse_hiring_cafe_html(html)
    return {"pages": 1, "errors": [], "stored": store_jobs(db, jobs)}


def ingest_abstractvc(db: Session, query: dict | None = None) -> dict:
    """Pull the SWE jobs from jobs.abstractvc.com (Abstract Ventures board)."""
    results: dict = {"errors": [], "pages": 0, "seen": 0}
    all_jobs: list[dict] = []
    with make_abstractvc_client() as client:
        try:
            token = bootstrap_token(client)
        except IngestionBlocked as exc:
            results["errors"].append(f"bootstrap: BLOCKED — {exc}")
            return results
        try:
            raw = fetch_all_jobs(client, token, query=query)
        except IngestionBlocked as exc:
            results["errors"].append(f"search: BLOCKED — {exc}")
            results["stored"] = store_jobs(db, all_jobs)
            return results
        except httpx.HTTPError as exc:
            results["errors"].append(f"search: {exc.__class__.__name__}: {str(exc)[:120]}")
            results["stored"] = store_jobs(db, all_jobs)
            return results
        all_jobs = parse_abstractvc_response({"jobs": raw})
        results["seen"] = len(all_jobs)
    results["stored"] = store_jobs(db, all_jobs)
    return results


def refilter_jobs(db: Session) -> dict:
    """Re-evaluate filter flags on all stored jobs (after filter rule changes)."""
    rows = db.query(Job).all()
    passing = 0
    for row in rows:
        keep, reasons = job_passes_filters(
            {
                "title": row.title,
                "company": row.company,
                "location": row.location,
                "countries": json.loads(row.countries_json) if row.countries_json else [],
                "remote": row.remote,
                "visa_sponsorship": row.visa_sponsorship,
                "yoe": row.yoe,
            }
        )
        row.passes_filters = keep
        row.filter_reasons_json = json.dumps(reasons)
        if keep:
            passing += 1
    db.commit()
    return {"rechecked": len(rows), "passing": passing}


def score_all_jobs(db: Session) -> dict:
    """(Re)score every stored job against the master resume (0-100)."""
    resume_skills = load_resume_skills(db)
    if not resume_skills:
        return {"scored": 0, "skipped": "no profile skills — seed the profile first"}
    rows = db.query(Job).all()
    avg = 0
    for row in rows:
        job_dict = {
            "title": row.title,
            "company": row.company,
            "location": row.location,
            "countries": json.loads(row.countries_json) if row.countries_json else [],
            "commitment": json.loads(row.commitment_json) if row.commitment_json else [],
            "remote": row.remote,
            "visa_sponsorship": row.visa_sponsorship,
            "yoe": row.yoe,
        }
        _score_row(row, job_dict, resume_skills)
        if row.match_score is not None:
            avg += row.match_score
    db.commit()
    scored = sum(1 for r in rows if r.match_score is not None)
    return {"scored": scored, "average": round(avg / scored) if scored else 0}


def ingest_company_feed_jobs(db: Session, limit: int = 25, delay: float = 1.5) -> dict:
    targets = (
        db.query(Company)
        .filter(Company.jobs_endpoint.isnot(None))
        .order_by(Company.id)
        .limit(limit)
        .all()
    )
    all_jobs: list[dict] = []
    errors: list[str] = []
    with make_client() as client:
        for comp in targets:
            try:
                jobs = fetch_jobs_for_endpoint(comp.ats, comp.jobs_endpoint or "", client)
                for j in jobs:
                    if not j.get("company"):
                        j["company"] = comp.name
                    if not j.get("company_website"):
                        j["company_website"] = comp.website
                all_jobs.extend(jobs)
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{comp.name}: {exc.__class__.__name__}")
            time.sleep(delay)
    stored = store_jobs(db, all_jobs)
    return {"companies": len(targets), "errors": errors, "stored": stored}
