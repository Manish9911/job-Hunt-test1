"""Seed the local profile from a resume file via the running API.

Usage (from the repo root, server must be running):
    uv run python scripts/seed_profile.py <path-to-resume.docx|pdf>

Reads the resume, uploads it to the app, and populates profile fields,
skills, work history, education, and starter common answers.
Idempotency: re-running duplicates entries — clear the data/ directory first
if you want a fresh seed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"

STARTER_ANSWERS = [
    ("Work authorization", "Are you authorized to work in the United States?", "Yes — I hold a valid US work visa (H1B/L1)."),
    ("Sponsorship", "Are you now or in the future in need of sponsorship?", "Yes — US work-authorization sponsorship is required."),
    ("Location", "Preferred work location / arrangement?", "US remote or onsite; Europe remote only."),
    ("Salary", "What are your salary expectations?", "(not set yet — pipeline will ask before submitting)"),
]


def main(path: str) -> None:
    resume_path = Path(path)
    if not resume_path.exists():
        sys.exit(f"File not found: {resume_path}")
    client = httpx.Client(base_url=BASE, timeout=60)
    try:
        client.get("/health")
    except httpx.HTTPError as exc:
        sys.exit(f"Server not reachable at {BASE}: {exc}")

    with resume_path.open("rb") as f:
        r = client.post("/api/resumes", files={"file": (resume_path.name, f)}, data={"label": "master"})
    r.raise_for_status()
    content = r.json()["content"]

    client.put("/api/profile", json={
        "full_name": content.get("name") or "",
        "email": content.get("email") or "",
        "phone": content.get("phone") or "",
        "summary": content.get("summary") or "",
        "work_authorization": "US work visa (H1B/L1)",
        "sponsorship_needed": True,
        "target_roles": "Any SWE role matching Java/Spring Boot/React/PostgreSQL/AWS stack; 4+ years experience",
        "locations": ["US remote", "US onsite", "EU remote"],
    })

    skill_count = 0
    for category, items in (content.get("skills") or {}).items():
        for name in items:
            client.post("/api/skills", json={"name": name, "category": category})
            skill_count += 1

    role_count = 0
    for w in content.get("work_history") or []:
        client.post("/api/work-history", json={
            "company": w.get("company"),
            "title": w.get("title", ""),
            "location": w.get("location") or None,
            "start_date": w.get("start", ""),
            "end_date": "" if w.get("is_current") else w.get("end", ""),
            "is_current": bool(w.get("is_current")),
            "bullets": w.get("bullets", []),
        })
        role_count += 1

    edu_count = 0
    for e in content.get("education") or []:
        client.post("/api/education", json={
            "school": e.get("school", ""),
            "degree": e.get("degree", ""),
            "field": e.get("field", ""),
            "start_date": e.get("start", ""),
            "end_date": e.get("end", ""),
        })
        edu_count += 1

    existing = {a["question"] for a in client.get("/api/common-answers").json()}
    answer_count = 0
    for category, q, a in STARTER_ANSWERS:
        if q not in existing:
            client.post("/api/common-answers", json={"category": category, "question": q, "answer": a})
            answer_count += 1

    print(f"Seeded: profile + resume {r.json()['id']} (master), {skill_count} skills, "
          f"{role_count} roles, {edu_count} education entries, {answer_count} starter answers.")
    print(f"Open {BASE}/ to review — edit anything that looks off.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
