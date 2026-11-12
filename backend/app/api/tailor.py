"""Phase 3 tailoring endpoints: tailor a job, fetch the result, export files."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.deps import get_db
from ..db.models import Job, Resume
from ..services import tailor
from ..services.export import export_docx, export_pdf

router = APIRouter(prefix="/api/jobs", tags=["tailoring"])


class TailorIn(BaseModel):
    jd_text: str | None = None
    cover_letter: bool = False


def _get_job_or_404(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/{job_id}/tailor")
def run_tailor(job_id: int, body: TailorIn, db: Session = Depends(get_db)):
    _get_job_or_404(db, job_id)
    try:
        return tailor.tailor_job(db, job_id, jd_text=body.jd_text,
                                 cover_letter=body.cover_letter)
    except tailor.TailoringError as exc:
        raise HTTPException(400, str(exc)) from exc


def tailored_view(db: Session, job: Job) -> dict:
    """Full tailoring result for a job (shared by the API endpoint and the page)."""
    if not job.tailored_resume_id:
        settings = Settings()
        llm_available = bool(settings.openai_api_key)
        return {
            "status": "not_tailored",
            "llm_available": llm_available,
            "hint": ("POST /api/jobs/%d/tailor to tailor this job" % job.id)
                    + ("" if llm_available else " (LLM not configured — deterministic reorder will be used)"),
        }
    res = db.get(Resume, job.tailored_resume_id)
    master = db.query(Resume).filter(Resume.is_master.is_(True)).first()
    tailored_structure = json.loads(res.content_json) if res and res.content_json else {}
    master_structure = json.loads(master.content_json) if master and master.content_json else {}
    diff = tailor.diff_master_tailored(master_structure, tailored_structure) if master_structure else ""
    try:
        notes = json.loads(job.tailor_notes_json) if job.tailor_notes_json else {}
    except json.JSONDecodeError:
        notes = {}
    return {
        "status": "tailored",
        "tailored_resume_id": res.id,
        "label": res.label,
        "structure": tailored_structure,
        "diff": diff,
        "match_score_before": job.match_score,
        "match_score_after": job.match_score_tailored,
        "lift": (job.match_score_tailored - job.match_score
                 if job.match_score is not None and job.match_score_tailored is not None else None),
        "cover_letter": job.cover_letter_text or None,
        "notes": notes,
        "exports": {
            "docx": f"/api/jobs/{job.id}/export/docx",
            "pdf": f"/api/jobs/{job.id}/export/pdf",
        },
    }


@router.get("/{job_id}/tailored")
def get_tailored(job_id: int, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    return tailored_view(db, job)


@router.get("/{job_id}/export/{fmt}")
def export_resume(
    job_id: int,
    fmt: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if fmt not in ("docx", "pdf"):
        raise HTTPException(400, "format must be docx or pdf")
    job = _get_job_or_404(db, job_id)
    target_id = job.tailored_resume_id
    kind = "tailored"
    if not target_id:
        master = db.query(Resume).filter(Resume.is_master.is_(True)).first()
        if master is None:
            raise HTTPException(404, "No resume to export")
        target_id, kind = master.id, "master"
    res = db.get(Resume, target_id)
    structure = json.loads(res.content_json) if res and res.content_json else {}
    if not structure:
        raise HTTPException(404, "Resume has no parsed content")

    out_dir = request.app.state.data_dir / "exports"
    # Naming (user rule 2026-11-06): "<user> <company> <role>.<fmt>" —
    # no resume_<kind> prefixes, no underscores or other symbols.
    from ..services.export import humanized_filename

    filename = humanized_filename(
        structure.get("name"), job.company, job.title, ext=fmt
    )
    path = out_dir / filename

    if fmt == "docx":
        export_docx(structure, path)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        export_pdf(structure, path)
        media = "application/pdf"
    return Response(
        content=path.read_bytes(),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
