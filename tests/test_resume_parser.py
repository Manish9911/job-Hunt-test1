from pathlib import Path

import pytest
from docx import Document

from backend.app.services.resume_parser import ParserError, parse_resume

REAL_RESUME = Path(r"C:\Users\badug\Downloads\Manish_Badugu_AWS_SDE_II_Original_Format.docx")

DOCX_LINES = [
    "Jane Doe",
    "Software Engineer",
    "+1 555-123-4567 | jane@example.com",
    "Professional Summary:",
    "Backend engineer with 5 years of experience in Java and Spring Boot.",
    "Technical Skills:",
    "Programming Languages: Java, Python",
    "Cloud Platforms: AWS",
    "Professional Experience:",
    "Acme Corp, Austin, TX\tJan 2022 - Present",
    "Senior Software Engineer",
    "Project Name: Payments Platform",
    "Built REST APIs with Spring Boot.",
    "Beta Inc, Remote\tMar 2020 - Dec 2021",
    "Software Engineer",
    "Shipped microservices on AWS.",
    "Education:",
    "Bachelor of Science, Computer Science — State University, Austin, TX | Aug 2016 - May 2020",
]


@pytest.fixture()
def docx_path(tmp_path):
    doc = Document()
    for line in DOCX_LINES:
        doc.add_paragraph(line)
    p = tmp_path / "jane.docx"
    doc.save(str(p))
    return p


def test_parse_docx_structure(docx_path):
    parsed = parse_resume(docx_path)
    assert parsed["name"] == "Jane Doe"
    assert parsed["email"] == "jane@example.com"
    assert parsed["phone"] == "+1 555-123-4567"
    assert "Backend engineer" in parsed["summary"]
    assert parsed["skills"]["Cloud Platforms"] == ["AWS"]
    assert "Java" in parsed["skills"]["Programming Languages"]

    hist = parsed["work_history"]
    assert len(hist) == 2
    assert hist[0]["company"] == "Acme Corp"
    assert hist[0]["location"] == "Austin, TX"
    assert hist[0]["title"] == "Senior Software Engineer"
    assert hist[0]["project"] == "Payments Platform"
    assert hist[0]["is_current"] is True
    assert hist[0]["bullets"] == ["Built REST APIs with Spring Boot."]
    assert hist[1]["company"] == "Beta Inc"
    assert hist[1]["is_current"] is False

    edu = parsed["education"]
    assert len(edu) == 1
    assert edu[0]["degree"] == "Bachelor of Science"
    assert edu[0]["field"] == "Computer Science"
    assert edu[0]["school"] == "State University, Austin, TX"
    assert edu[0]["start"] == "Aug 2016"
    assert edu[0]["end"] == "May 2020"


def test_parse_real_master_resume():
    if not REAL_RESUME.exists():
        pytest.skip("real master resume not present on this machine")
    parsed = parse_resume(REAL_RESUME)
    companies = [h["company"] for h in parsed["work_history"]]
    assert any("Verizon" in c for c in companies)
    assert any("Equifax" in c for c in companies)
    assert any("Polaris" in c for c in companies)
    assert parsed["skills"].get("Programming Languages")
    assert len(parsed["education"]) >= 2
    assert parsed["email"] == "badugu.m7@gmail.com"


def test_unsupported_extension(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hello", encoding="utf-8")
    with pytest.raises(ParserError):
        parse_resume(p)


def test_missing_file(tmp_path):
    with pytest.raises(ParserError):
        parse_resume(tmp_path / "nope.docx")
