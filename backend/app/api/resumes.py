import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..db.deps import get_db, get_profile
from ..db.models import Profile, Resume
from ..services.resume_parser import ParserError, parse_resume

router = APIRouter(prefix="/api/resumes", tags=["resumes"])

ALLOWED_SUFFIXES = {".docx", ".pdf"}


def _safe_name(filename: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:120] or "resume"


def _resume_dir(request: Request) -> Path:
    d = request.app.state.data_dir / "resumes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_or_404(db: Session, profile: Profile, resume_id: int) -> Resume:
    row = db.get(Resume, resume_id)
    if row is None or row.profile_id != profile.id:
        raise HTTPException(404, "Resume not found")
    return row


def _meta(r: Resume) -> dict:
    return {
        "id": r.id,
        "label": r.label,
        "original_filename": r.original_filename,
        "is_master": r.is_master,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("", status_code=201)
def upload_resume(
    request: Request,
    file: UploadFile = File(...),
    label: str = Form(""),
    profile: Profile = Depends(get_profile),
    db: Session = Depends(get_db),
):
    name = _safe_name(file.filename or "resume")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, f"Unsupported file type {suffix!r}; use .docx or .pdf")

    data = file.file.read()
    if not data:
        raise HTTPException(400, "Empty file")

    storage_path = _resume_dir(request) / f"{uuid.uuid4().hex}{suffix}"
    storage_path.write_bytes(data)
    try:
        content = parse_resume(storage_path)
    except ParserError as exc:
        storage_path.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not parse resume: {exc}")

    existing = db.query(Resume).filter(Resume.profile_id == profile.id).count()
    row = Resume(
        profile_id=profile.id,
        label=label.strip() or name,
        original_filename=file.filename or name,
        storage_path=str(storage_path),
        content_json=json.dumps(content, indent=2),
        is_master=existing == 0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {**_meta(row), "content": content}


@router.get("")
def list_resumes(profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    rows = db.query(Resume).filter(Resume.profile_id == profile.id).order_by(Resume.id).all()
    return [_meta(r) for r in rows]


@router.get("/{resume_id}")
def get_resume(resume_id: int, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    row = _get_or_404(db, profile, resume_id)
    try:
        content = json.loads(row.content_json)
    except json.JSONDecodeError:
        content = {}
    return {**_meta(row), "content": content}


@router.get("/{resume_id}/download")
def download_resume(resume_id: int, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    row = _get_or_404(db, profile, resume_id)
    path = Path(row.storage_path)
    if not path.exists():
        raise HTTPException(410, "Stored file no longer exists")
    return FileResponse(path, filename=row.original_filename)


@router.post("/{resume_id}/master")
def set_master(resume_id: int, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    row = _get_or_404(db, profile, resume_id)
    for other in db.query(Resume).filter(
        Resume.profile_id == profile.id, Resume.is_master.is_(True)
    ).all():
        other.is_master = False
    row.is_master = True
    db.commit()
    return {**_meta(row)}


@router.delete("/{resume_id}")
def delete_resume(resume_id: int, profile: Profile = Depends(get_profile), db: Session = Depends(get_db)):
    row = _get_or_404(db, profile, resume_id)
    was_master = row.is_master
    remaining = (
        db.query(Resume).filter(Resume.profile_id == profile.id).order_by(Resume.id.desc()).all()
    )
    Path(row.storage_path).unlink(missing_ok=True)
    db.delete(row)
    if was_master and remaining:
        remaining[0].is_master = True
    db.commit()
    return {"deleted": resume_id}
