def test_skills_crud(client):
    r = client.post("/api/skills", json={"name": "Spring Boot", "category": "Backend"})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert {"id": sid, "name": "Spring Boot", "category": "Backend"} in client.get("/api/skills").json()

    r = client.put(f"/api/skills/{sid}", json={"name": "Spring Boot 3", "category": "Backend"})
    assert r.json()["name"] == "Spring Boot 3"

    assert client.delete(f"/api/skills/{sid}").status_code == 200
    assert client.get("/api/skills").json() == []
    assert client.delete(f"/api/skills/{sid}").status_code == 404


def test_skill_requires_name(client):
    assert client.post("/api/skills", json={"name": "  ", "category": "X"}).status_code == 400


def test_work_history_accepts_text_bullets(client):
    r = client.post("/api/work-history", json={
        "company": "Acme",
        "title": "SWE",
        "location": "Remote",
        "start_date": "Jan 2022",
        "is_current": True,
        "bullets": "Built APIs\nFixed bugs",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["bullets"] == ["Built APIs", "Fixed bugs"]
    assert body["is_current"] is True

    listing = client.get("/api/work-history").json()
    assert listing[0]["company"] == "Acme"
    assert listing[0]["bullets"] == ["Built APIs", "Fixed bugs"]


def test_work_history_checkbox_string(client):
    r = client.post("/api/work-history", json={"company": "Acme", "is_current": "true"})
    assert r.json()["is_current"] is True
    r = client.post("/api/work-history", json={"company": "Beta"})
    assert r.json()["is_current"] is False


def test_education_crud(client):
    r = client.post("/api/education", json={
        "school": "State University",
        "degree": "Bachelor of Science",
        "field": "Computer Science",
        "start_date": "Aug 2016",
        "end_date": "May 2020",
    })
    assert r.status_code == 201
    eid = r.json()["id"]
    assert len(client.get("/api/education").json()) == 1
    assert client.delete(f"/api/education/{eid}").status_code == 200
    assert client.get("/api/education").json() == []


def test_common_answers_crud(client):
    r = client.post("/api/common-answers", json={
        "category": "Work authorization",
        "question": "Are you authorized to work in the US?",
        "answer": "Yes — H1B visa.",
    })
    assert r.status_code == 201
    aid = r.json()["id"]
    rows = client.get("/api/common-answers").json()
    assert len(rows) == 1 and rows[0]["answer"] == "Yes — H1B visa."

    r = client.put(f"/api/common-answers/{aid}", json={
        "category": "Work authorization",
        "question": "Are you authorized to work in the US?",
        "answer": "Yes — L1 visa.",
    })
    assert r.json()["answer"] == "Yes — L1 visa."
    assert client.delete(f"/api/common-answers/{aid}").status_code == 200
    assert client.get("/api/common-answers").json() == []
