"""FastAPI application factory for jobflow."""
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import pages
from .api import common_answers, education, jobs, profile, resumes, skills, work_history, tailor
from .config import Settings
from .db import models  # noqa: F401  (register models on Base)
from .db.base import Base, ensure_columns, make_engine, make_session

ROOT = Path(__file__).resolve().parents[2]


def create_app(database_url: str | None = None) -> FastAPI:
    settings = Settings()
    db_url = database_url or settings.database_url

    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    ensure_columns(engine)

    app = FastAPI(title="jobflow", version="0.1.0")
    app.state.engine = engine
    app.state.SessionLocal = make_session(engine)
    app.state.data_dir = data_dir

    templates = Jinja2Templates(directory=str(ROOT / "frontend" / "templates"))
    templates.env.filters["from_json_list"] = lambda s: json.loads(s) if s else []
    templates.env.filters["from_json"] = lambda s: json.loads(s) if s else {}
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(ROOT / "frontend" / "static")), name="static")

    for router in (
        pages.router,
        profile.router,
        resumes.router,
        skills.router,
        work_history.router,
        education.router,
        common_answers.router,
        jobs.router,
        jobs.router_public,
        tailor.router,
    ):
        app.include_router(router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "phase": 1}

    return app


app = create_app()
