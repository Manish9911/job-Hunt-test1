"""Tests for the 0-100 resume-vs-JD match scorer and its dashboard wiring."""
from backend.app.ingestion.match import score_job, skill_overlap, score_lift

MASTER_SKILLS = [
    "Java", "Spring Boot", "React.js", "PostgreSQL", "SQL",
    "REST APIs", "Microservices", "Amazon Web Services (AWS)", "JavaScript",
]


def seed_profile(app, skills=MASTER_SKILLS):
    """Create a profile + skills directly via ORM (faster than the API)."""
    from backend.app.db.models import Profile, Skill

    Session = app.state.SessionLocal
    db = Session()
    profile = Profile(full_name="Test User", email="t@example.com")
    db.add(profile)
    db.commit()
    for name in skills:
        db.add(Skill(profile_id=profile.id, name=name))
    db.commit()
    db.close()
    return profile.id


def job(**kw):
    base = {
        "title": "Senior Software Engineer - Payments",
        "company": "Acme",
        "location": "Austin, TX",
        "countries": ["US"],
        "commitment": ["Full Time"],
        "remote": True,
        "visa_sponsorship": True,
        "yoe": 3,
    }
    base.update(kw)
    return base


# ---------- deterministic scorer ----------

def test_strong_match_scores_high():
    # Java/Spring/Postgres JD vs a Java/Spring/React/Postgres resume
    j = job(title="Senior Java Software Engineer (Spring Boot, PostgreSQL, AWS)")
    score, bd = score_job(j, MASTER_SKILLS)
    assert score >= 75, bd
    assert bd["skills"]["points"] == bd["skills"]["max"]
    assert "java" in bd["skills"]["matched"]
    assert "postgresql" in bd["skills"]["matched"]
    assert bd["total"] == score
    assert 0 <= score <= 100


def test_weak_stack_match_scores_lower():
    strong, _ = score_job(
        job(title="Senior Java Software Engineer (Spring Boot, PostgreSQL, AWS)"),
        MASTER_SKILLS,
    )
    weak, bd = score_job(job(title="Senior Software Engineer (Ruby on Rails)"), MASTER_SKILLS)
    assert weak < strong
    assert bd["skills"]["points"] < bd["skills"]["max"]


def test_non_swe_title_loses_role_points():
    swe, swe_bd = score_job(job(title="Senior Software Engineer"), MASTER_SKILLS)
    other, other_bd = score_job(job(title="Senior Accountant"), MASTER_SKILLS)
    assert swe_bd["role"]["points"] > other_bd["role"]["points"]
    assert swe > other


def test_high_yoe_requirement_loses_experience_points():
    ok, ok_bd = score_job(job(yoe=3), MASTER_SKILLS)
    senior, senior_bd = score_job(job(yoe=10), MASTER_SKILLS)
    assert ok_bd["role"]["experience_ok"] is True
    assert senior_bd["role"]["experience_ok"] is False
    assert ok > senior


def test_unknown_yoe_is_neutral():
    known, _ = score_job(job(yoe=3), MASTER_SKILLS)
    unknown, bd = score_job(job(yoe=None), MASTER_SKILLS)
    assert bd["role"]["experience_ok"] is True
    assert unknown <= known


def test_eu_onsite_scores_lower_meta_than_remote():
    eu_remote, bd_remote = score_job(
        job(location="Amsterdam", countries=["NL"], remote=True), MASTER_SKILLS
    )
    eu_onsite, bd_onsite = score_job(
        job(location="Amsterdam", countries=["NL"], remote=False), MASTER_SKILLS
    )
    assert bd_remote["meta"]["points"] > bd_onsite["meta"]["points"]
    assert eu_remote > eu_onsite


def test_us_no_visa_sponsorship_scores_lower_meta():
    sponsored, bd_s = score_job(job(visa_sponsorship=True), MASTER_SKILLS)
    not_sponsored, bd_n = score_job(job(visa_sponsorship=False), MASTER_SKILLS)
    assert bd_s["meta"]["points"] > bd_n["meta"]["points"]
    assert sponsored > not_sponsored


def test_score_sum_of_axes():
    score, bd = score_job(job(), MASTER_SKILLS)
    total = sum(bd[axis]["points"] for axis in ("skills", "role", "meta"))
    assert score == min(100, total)
    assert bd["method"] == "deterministic-v1"


def test_no_recognized_skills_in_jd_gives_neutral_skills_axis():
    _, bd = score_job(job(title="Senior Software Engineer"), MASTER_SKILLS)
    # title has no stack keywords -> neutral half of the skills axis
    assert bd["skills"]["points"] == round(60 * 0.5)


def test_skill_overlap_counts():
    hits, total, matched = skill_overlap(
        job(title="Java Developer (Spring, React)"), MASTER_SKILLS
    )
    assert "java" in matched and "react" in matched
    assert hits == len(matched)
    assert total >= hits


def test_score_lift():
    assert score_lift(80, 90) == 10
    assert score_lift(80, 70) == -10
    assert score_lift(80, None) is None
    assert score_lift(None, 90) is None


# ---------- DB + dashboard wiring ----------

def test_store_jobs_auto_scores_with_profile_skills(app, client):
    seed_profile(app)
    html = """
    <script id="__NEXT_DATA__" type="application/json">
    {"props":{"pageProps":{"ssrHits":[
      {"job_information":{"title":"Senior Java Software Engineer (Spring Boot, PostgreSQL)","job_title_raw":"Senior Java Software Engineer (Spring Boot, PostgreSQL)"},"v5_processed_job_data":{"company_name":"Acme","formatted_workplace_location":"Austin, Texas","workplace_countries":["US"],"workplace_type":"remote","visa_sponsorship":true,"min_industry_and_role_yoe":3,"commitment":["Full Time"],"yearly_min_compensation":150,"yearly_max_compensation":180,"listed_compensation_currency":"USD","listed_compensation_frequency":"Yearly"},"attributed_org":{"name":"Acme","website":"acme.com"},"apply_url":"https://acme.com/jobs/1","source":"acme-feed","objectID":"obj-java","hc_indexed_at":1730000000}
    ],"ssrPage":1,"ssrPageSize":10,"ssrTotalCount":1,"ssrIsLastPage":true}}}
    </script>
    <html><body><p>hiring.cafe jobs page content padding to pass the minimum-size
    check for manual ingestion in jobflow tests. More padding text so the payload
    exceeds five hundred characters of saved page source as a user would provide.</p></body></html>
    """
    r = client.post("/api/ingest/jobs/hiring-cafe/manual", json={"html": html})
    assert r.status_code == 200, r.text
    r = client.get("/api/jobs")
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1
    j = jobs[0]
    assert j["match_score"] is not None
    assert 0 <= j["match_score"] <= 100
    assert j["match_score_tailored"] is None  # Phase 3 fills this
    assert j["match_lift"] is None
    assert j["match_breakdown"]["method"] == "deterministic-v1"
    assert j["apply_url"] == "https://acme.com/jobs/1"


def test_jobs_page_defaults_to_matched_only(app, client):
    seed_profile(app)
    Session = app.state.SessionLocal
    db = Session()
    from backend.app.db.models import Job

    db.add(Job(title="Senior Java Software Engineer", company="A", location="Austin, TX",
               countries_json='["US"]', remote=True, visa_sponsorship=True,
               passes_filters=True, unique_key="k1", apply_url="https://a.example/1"))
    db.add(Job(title="Data Entry Clerk", company="B", location="Chicago, IL",
               countries_json='["US"]', remote=None, visa_sponsorship=None,
               passes_filters=False, filter_reasons_json='["title is not an SWE/software role"]',
               unique_key="k2", apply_url="https://b.example/2"))
    db.commit()
    db.close()

    default_html = client.get("/jobs").text
    assert "Senior Java Software Engineer" in default_html
    assert "Data Entry Clerk" not in default_html  # non-matched hidden by default
    assert "Matched jobs" in default_html

    all_html = client.get("/jobs?all=1").text
    assert "Data Entry Clerk" in all_html  # shown with the all=1 toggle
    assert "All jobs" in all_html


def test_rescore_endpoint_scores_all_jobs(app, client):
    seed_profile(app)
    Session = app.state.SessionLocal
    db = Session()
    from backend.app.db.models import Job

    db.add(Job(title="Senior Java Software Engineer (Spring, React)", company="A",
               location="Austin, TX", countries_json='["US"]', remote=True,
               visa_sponsorship=True, passes_filters=True, unique_key="k1",
               apply_url="https://a.example/1"))
    db.commit()
    db.close()

    r = client.post("/api/ingest/jobs/score")
    assert r.status_code == 200
    assert r.json()["scored"] == 1
    assert 0 <= r.json()["average"] <= 100

    j = client.get("/api/jobs?limit=1").json()[0]
    assert j["match_score"] is not None
