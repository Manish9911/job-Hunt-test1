"""Phase 3 tests: LLM interface, truth validation, tailoring pipeline, exports."""
import json

import httpx
import pytest

from backend.app.db.models import Job, Profile, Resume
from backend.app.ingestion.match import SKILL_VOCAB
from backend.app.llm import OpenAIProvider
from backend.app.llm.base import LLMUnavailable
from backend.app.services import tailor
from backend.app.services.tailor import (
    deterministic_tailor,
    fetch_jd,
    render_resume_text,
    validate_tailored,
)

MASTER = {
    "name": "Test Dev",
    "email": "t@example.com",
    "phone": "555-0100",
    "summary": "Java and React developer with 4+ years of experience.",
    "skills": {
        "Languages": ["Java", "JavaScript", "SQL"],
        "Backend": ["Spring Boot", "REST APIs", "Microservices"],
        "Cloud": ["Amazon Web Services (AWS)"],
        "Frontend": ["React.js"],
    },
    "work_history": [{
        "company": "Acme Corp", "title": "Software Engineer", "location": "Austin, TX",
        "start": "Jan 2022", "end": "Present", "project": "Payments",
        "bullets": [
            "Wrote SQL queries and tuned PostgreSQL reports.",
            "Built REST APIs in Java and Spring Boot.",
            "Improved React dashboards for operations teams.",
        ],
    }],
    "education": [{
        "degree": "Master of Science", "field": "Computer Science",
        "school": "Test University", "start": "Jan 2020", "end": "May 2022",
    }],
}

JD = ("Senior Java Software Engineer. You will build REST APIs with Java, Spring Boot, "
      "and PostgreSQL, deploy on AWS, and improve our React frontend. 4+ years required.")


def seed_master(app) -> int:
    Session = app.state.SessionLocal
    db = Session()
    p = Profile(full_name="Test Dev", email="t@example.com")
    db.add(p)
    db.commit()
    r = Resume(profile_id=p.id, label="master", original_filename="m.docx",
               storage_path="", content_json=json.dumps(MASTER), is_master=True)
    db.add(r)
    db.commit()
    rid = r.id
    db.close()
    return rid


def make_job(app, apply_url="", **kw) -> int:
    Session = app.state.SessionLocal
    db = Session()
    j = Job(title="Senior Java Engineer", company="Acme", location="Austin, TX",
            countries_json='["US"]', remote=True, visa_sponsorship=True,
            passes_filters=True, unique_key=kw.pop("unique_key", "t1"),
            apply_url=apply_url, **kw)
    db.add(j)
    db.commit()
    jid = j.id
    db.close()
    return jid


class FakeLLM:
    """Scripted LLM for tests: pops responses in call order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.available = True

    def is_available(self):
        return self.available

    def complete(self, system, user, temperature=0.2, timeout=90.0):
        self.calls.append((system, user))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# ---------- LLM provider ----------

def test_openai_provider_unavailable_without_key():
    p = OpenAIProvider("", "gpt-4o-mini")
    assert p.is_available() is False
    with pytest.raises(LLMUnavailable):
        p.complete("s", "u")


def test_openai_provider_parses_response(monkeypatch):
    p = OpenAIProvider("sk-test", "gpt-4o-mini")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["auth"] = headers["Authorization"]
        captured["body"] = json

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "hello from llm"}}]}

        return R()

    import backend.app.llm as llm_pkg
    monkeypatch.setattr(llm_pkg.httpx, "post", fake_post)
    assert p.complete("sys", "user") == "hello from llm"
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["model"] == "gpt-4o-mini"
    assert captured["body"]["messages"][0]["content"] == "sys"


# ---------- truth validation ----------

def test_validate_accepts_truthful_reorder():
    t = json.loads(json.dumps(MASTER))
    t["skills"] = {"Backend": ["Spring Boot", "Java"], "Languages": ["SQL", "JavaScript"]}
    assert validate_tailored(MASTER, t) == []


def test_validate_rejects_invented_employer():
    t = json.loads(json.dumps(MASTER))
    t["work_history"][0]["company"] = "MegaCorp Inc"
    v = validate_tailored(MASTER, t)
    assert any("MegaCorp" in x for x in v)


def test_validate_rejects_invented_date():
    t = json.loads(json.dumps(MASTER))
    t["work_history"][0]["start"] = "Mar 2019"
    v = validate_tailored(MASTER, t)
    assert any("Mar 2019" in x for x in v)


def test_validate_rejects_invented_skill():
    t = json.loads(json.dumps(MASTER))
    t["skills"]["Languages"] = ["Java", "Rust"]
    v = validate_tailored(MASTER, t)
    assert any("Rust" in x for x in v)


def test_validate_allows_synonyms():
    t = json.loads(json.dumps(MASTER))
    t["skills"]["Frontend"] = ["React"]  # master has React.js
    assert validate_tailored(MASTER, t) == []


def test_validate_rejects_changed_contact():
    t = json.loads(json.dumps(MASTER))
    t["phone"] = "999-9999"
    assert any("phone" in x for x in validate_tailored(MASTER, t))


def test_validate_rejects_invented_degree():
    t = json.loads(json.dumps(MASTER))
    t["education"][0]["degree"] = "Doctor of Philosophy"
    assert any("Doctor of Philosophy" in x for x in validate_tailored(MASTER, t))


# ---------- deterministic tailoring ----------

def test_deterministic_tailor_changes_no_facts():
    t, note = deterministic_tailor(MASTER, JD)
    assert validate_tailored(MASTER, t) == []
    # same employers, dates, skills set, bullets set
    assert {j["company"] for j in t["work_history"]} == {j["company"] for j in MASTER["work_history"]}
    m_skills = {s for v in MASTER["skills"].values() for s in v}
    t_skills = {s for v in t["skills"].values() for s in v}
    assert m_skills == t_skills
    mb = MASTER["work_history"][0]["bullets"]
    tb = t["work_history"][0]["bullets"]
    assert set(mb) == set(tb)
    # summary/education untouched
    assert t["summary"] == MASTER["summary"]
    assert t["education"] == MASTER["education"]
    assert "deterministic" in note


def test_deterministic_tailor_prioritizes_jd_skills():
    t, _ = deterministic_tailor(MASTER, JD)
    # JD mentions java/spring/sql/aws/react -> JD-relevant categories float to the top
    assert list(t["skills"].keys())[0] == "Backend"  # most JD keyword hits
    all_skills = [s for v in t["skills"].values() for s in v]
    assert "Java" in all_skills[:4]
    # Java/Spring bullet should rank first
    assert t["work_history"][0]["bullets"][0].startswith("Built REST APIs")


# ---------- pipeline (service level) ----------

def _valid_tailored_output() -> str:
    t = json.loads(json.dumps(MASTER))
    t["skills"] = {"Backend": ["Java", "Spring Boot", "SQL"],
                   "Cloud": ["Amazon Web Services (AWS)"],
                   "Frontend": ["React.js"],
                   "Languages": ["JavaScript"]}
    t["work_history"][0]["bullets"] = [
        "Built REST APIs in Java and Spring Boot.",
        "Wrote SQL queries and tuned PostgreSQL reports.",
        "Improved React dashboards for operations teams.",
    ]
    return json.dumps(t)


def test_tailor_deterministic_path_without_llm(app):
    seed_master(app)
    jid = make_job(app)
    llm = FakeLLM([])
    llm.available = False
    result = tailor.tailor_job(app.state.SessionLocal(), jid, jd_text=JD, llm=llm)
    assert result["method"] == "deterministic"
    assert result["match_score_before"] is not None
    assert result["match_score_after"] is not None
    # deterministic: JD skills are covered by master, so both scores should be high
    assert result["match_score_before"] >= 60

    Session = app.state.SessionLocal
    db = Session()
    job = db.get(Job, jid)
    res = db.get(Resume, job.tailored_resume_id)
    assert res is not None and res.is_master is False
    t = json.loads(res.content_json)
    assert validate_tailored(MASTER, t) == []
    assert job.match_score_tailored == result["match_score_after"]
    db.close()


def test_tailor_llm_path_happy(app):
    seed_master(app)
    jid = make_job(app)
    llm = FakeLLM([
        json.dumps({"score": 78, "reasoning": "strong java/spring fit",
                    "strongest_matches": ["Java", "Spring Boot"], "gaps": ["K8s"]}),
        _valid_tailored_output(),
        json.dumps({"score": 86, "reasoning": "tailored emphasis improves fit",
                    "strongest_matches": ["Java", "Spring Boot", "React"], "gaps": []}),
    ])
    result = tailor.tailor_job(app.state.SessionLocal(), jid, jd_text=JD, llm=llm)
    assert result["method"] == "llm"
    assert result["match_score_before"] == 78
    assert result["match_score_after"] == 86
    assert result["lift"] == 8
    assert len(llm.calls) == 3

    # the tailor prompt carried the truth contract + the master resume facts
    tailor_prompt_system, tailor_prompt_user = llm.calls[1]
    assert "NEVER invent" in tailor_prompt_system
    assert "reorder, reword, and emphasize" in tailor_prompt_system
    assert "Acme Corp" in tailor_prompt_user  # master facts provided
    assert "Senior Java Software Engineer" in tailor_prompt_user  # JD provided


def test_tailor_llm_hallucination_falls_back(app):
    seed_master(app)
    jid = make_job(app)
    bad = json.loads(json.dumps(MASTER))
    bad["work_history"][0]["company"] = "MegaCorp Inc"  # invented employer
    llm = FakeLLM([
        json.dumps({"score": 70, "reasoning": "ok", "strongest_matches": [], "gaps": []}),
        json.dumps(bad),  # attempt 1: hallucination
        json.dumps(bad),  # attempt 2: hallucination again
        json.dumps({"score": 60, "reasoning": "fallback", "strongest_matches": [], "gaps": []}),
    ])
    result = tailor.tailor_job(app.state.SessionLocal(), jid, jd_text=JD, llm=llm)
    assert result["method"] == "deterministic-fallback"
    assert result["match_score_after"] == 60

    Session = app.state.SessionLocal
    db = Session()
    job = db.get(Job, jid)
    notes = json.loads(job.tailor_notes_json)
    assert any("MegaCorp" in v for v in notes["rejected_violations"])
    t = json.loads(db.get(Resume, job.tailored_resume_id).content_json)
    # the stored artifact must NOT contain the hallucinated employer
    assert all(j["company"] != "MegaCorp Inc" for j in t["work_history"])
    assert validate_tailored(MASTER, t) == []
    db.close()


def test_tailor_requires_jd(app):
    seed_master(app)
    jid = make_job(app, unique_key="nojd1", apply_url="")
    with pytest.raises(tailor.TailoringError):
        tailor.tailor_job(app.state.SessionLocal(), jid, jd_text=None,
                          llm=FakeLLM([]))


def test_cover_letter_written_with_llm(app):
    seed_master(app)
    jid = make_job(app)
    llm = FakeLLM([
        json.dumps({"score": 78, "reasoning": "x", "strongest_matches": [], "gaps": []}),
        _valid_tailored_output(),
        json.dumps({"score": 84, "reasoning": "y", "strongest_matches": [], "gaps": []}),
        "Dear hiring team at Acme, ... (170 words of truthful facts only)",
    ])
    result = tailor.tailor_job(app.state.SessionLocal(), jid, jd_text=JD,
                               cover_letter=True, llm=llm)
    assert result["cover_letter"] is True
    assert len(llm.calls) == 4
    cover_system, cover_user = llm.calls[3]
    assert "ONLY facts" in cover_system


# ---------- JD fetching ----------

def _job_obj(apply_url: str) -> Job:
    return Job(apply_url=apply_url)


def test_fetch_jd_greenhouse(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v1/boards/acme/jobs/123" in str(request.url)
        return httpx.Response(200, json={"content": "<p>Build Java services with Spring Boot</p>"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    jd = fetch_jd(_job_obj("https://job-boards.greenhouse.io/acme/jobs/123"), client=client)
    assert "Java services" in jd and "<" not in jd


def test_fetch_jd_lever(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/postings/acme" in str(request.url)
        return httpx.Response(200, json=[{"id": "abc123", "description": "<p>Lever Java role</p>"}])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    jd = fetch_jd(_job_obj("https://jobs.lever.co/acme/abc123"), client=client)
    assert "Lever Java role" in jd


def test_fetch_jd_generic_page(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body><div>Full JD text here</div></body></html>")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    jd = fetch_jd(_job_obj("https://acme.com/careers/9"), client=client)
    assert "Full JD text here" in jd


def test_fetch_jd_failure_returns_empty(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch_jd(_job_obj("https://acme.com/careers/9"), client=client) == ""


def test_fetch_jd_no_url(app):
    assert fetch_jd(_job_obj("")) == ""


# ---------- API + exports ----------

def test_api_tailor_and_export_endpoints(app, client):
    seed_master(app)
    jid = make_job(app)
    # no LLM key in test env -> deterministic mode
    r = client.post(f"/api/jobs/{jid}/tailor", json={"jd_text": JD, "cover_letter": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "deterministic"
    assert body["match_score_before"] is not None

    r = client.get(f"/api/jobs/{jid}/tailored")
    assert r.status_code == 200
    t = r.json()
    assert t["status"] == "tailored"
    assert t["diff"]  # non-empty unified diff
    assert "master resume" in t["diff"] and "tailored resume" in t["diff"]

    r = client.get(f"/api/jobs/{jid}/export/docx")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"  # docx is a zip
    assert "wordprocessingml" in r.headers["content-type"]

    r = client.get(f"/api/jobs/{jid}/export/pdf")
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"

    r = client.get(f"/api/jobs/{jid}/export/xyz")
    assert r.status_code == 400


def test_api_tailor_missing_jd_is_400(app, client):
    seed_master(app)
    jid = make_job(app, unique_key="nojd2", apply_url="")
    r = client.post(f"/api/jobs/{jid}/tailor", json={})
    assert r.status_code == 400
    assert "job description" in r.json()["detail"].lower() or "paste" in r.json()["detail"].lower()


def test_api_tailored_before_tailoring_is_status(app, client):
    seed_master(app)
    jid = make_job(app)
    r = client.get(f"/api/jobs/{jid}/tailored")
    assert r.status_code == 200
    assert r.json()["status"] == "not_tailored"
    assert isinstance(r.json()["llm_available"], bool)


def test_job_detail_page_renders_tailoring_card(app, client):
    seed_master(app)
    jid = make_job(app)
    html = client.get(f"/jobs/{jid}").text
    assert "Resume tailoring" in html
    assert "Tailor resume for this job" in html  # not-tailored form
    client.post(f"/api/jobs/{jid}/tailor", json={"jd_text": JD})
    html = client.get(f"/jobs/{jid}").text
    assert "Download tailored DOCX" in html
    assert "Diff vs master resume" in html


def test_prompt_contract_mentions_hard_rules():
    assert "NEVER invent" in tailor.TAILOR_SYSTEM
    assert "reorder, reword, and emphasize" in tailor.TAILOR_SYSTEM
    assert "employers" in tailor.TAILOR_SYSTEM
    assert "dates" in tailor.TAILOR_SYSTEM
    assert "degrees" in tailor.TAILOR_SYSTEM


def test_render_resume_text_contains_facts():
    text = render_resume_text(MASTER)
    for fact in ("Test Dev", "Acme Corp", "Jan 2022", "Master of Science",
                 "Spring Boot", "Test University"):
        assert fact in text


def test_skill_vocab_covers_confirmed_stack():
    for term in ("java", "spring", "react", "postgres", "sql", "aws", "rest",
                 "microservice", "javascript"):
        assert any(term in label or term in (v.pattern or "") for label, v in SKILL_VOCAB.items()), term
