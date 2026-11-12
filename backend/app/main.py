"""FastAPI application factory for jobflow."""
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import pages
from .api import (
    applications,
    common_answers,
    education,
    jobs,
    lessons,
    profile,
    resumes,
    skills,
    tailor,
    work_history,
)
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
        applications.router,
        lessons.router,
    ):
        app.include_router(router)

    from .api import agent as agent_api
    app.include_router(agent_api.router)
    from .api import notifications as notifications_api
    app.include_router(notifications_api.router)
    from .services.agent import AutoApplyAgent
    agent_state = AutoApplyAgent()
    agent_state.session_factory = make_session(engine)
    agent_state.data_dir = data_dir
    app.state.agent = agent_state

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "phase": 1}

    return app


app = create_app()
