"""Best-effort resume parsing: DOCX/PDF -> structured JSON.

Layout-tolerant heuristics. The structured output is always editable in
the dashboard; nothing here is treated as ground truth downstream.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?:\+\d{1,2}[\s\-]?)?(?:\(\d{3}\)|\d{3})[\s\-]?\d{3}[\s\-]?\d{4}")

_M = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DATE_RANGE_RE = re.compile(
    rf"({_M}\.?\s+\d{{4}}|\d{{4}})\s*[–—\-]\s*(Present|Current|Now|Today|{_M}\.?\s+\d{{4}}|\d{{4}})",
    re.IGNORECASE,
)
DEGREE_RE = re.compile(
    r"(master|bachelor|associate|m\.?s\.?c?|b\.?s\.?c?|beng|meng|phd|b\.?e\.?|msc|ms\b|bs\b|ba\b)",
    re.IGNORECASE,
)
PROJECT_RE = re.compile(r"^project\s*(name|title)?\s*[:\-]?\s*", re.IGNORECASE)
SECTION_RE = {
    "summary": re.compile(r"^(professional\s+)?(summary|profile|objective)\s*:?\s*$", re.IGNORECASE),
    "skills": re.compile(
        r"^(technical\s+|core\s+|relevant\s+)?(skills|technologies|tech\s*stack)\s*:?\s*$",
        re.IGNORECASE,
    ),
    "experience": re.compile(
        r"^(professional\s+|work\s+)?(experience|employment|work\s+history)\s*:?\s*$", re.IGNORECASE
    ),
    "education": re.compile(r"^education(al)?\s*:?\s*$", re.IGNORECASE),
}


class ParserError(Exception):
    pass


def parse_resume(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ParserError(f"File not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".docx":
        text = _extract_docx(p)
    elif suffix == ".pdf":
        text = _extract_pdf(p)
    else:
        raise ParserError(f"Unsupported type: {suffix}")
    lines = [ln.strip() for ln in text.splitlines()]
    if not any(lines):
        raise ParserError("No text extracted from file")
    return _structure(lines)


def _extract_docx(path: Path) -> str:
    try:
        import docx  # python-docx
    except ImportError:
        docx = None
    if docx is not None:
        document = docx.Document(str(path))
        return "\n".join(p.text for p in document.paragraphs)
    # Fallback: raw OOXML (no third-party dependency)
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    return "\n".join(
        "".join(t.text or "" for t in p.iter(f"{ns}t")) for p in root.iter(f"{ns}p")
    )


def _extract_pdf(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError:
        raise ParserError("pdfplumber is required to parse PDF resumes")
    with pdfplumber.open(str(path)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def _structure(lines: list[str]) -> dict:
    lines = [ln for ln in lines if ln.strip()]
    result: dict = {
        "name": lines[0],
        "email": None,
        "phone": None,
        "summary": "",
        "skills": {},
        "work_history": [],
        "education": [],
    }
    for ln in lines[:4]:
        if result["email"] is None:
            m = EMAIL_RE.search(ln)
            if m:
                result["email"] = m.group(0)
        if result["phone"] is None:
            m = PHONE_RE.search(ln)
            if m:
                result["phone"] = m.group(0)

    section = None
    summary_parts: list[str] = []
    current: dict | None = None

    for ln in lines[1:]:
        matched = None
        for key, rx in SECTION_RE.items():
            if rx.match(ln):
                matched = key
                break
        if matched:
            section = matched
            current = None
            continue

        if section == "summary":
            summary_parts.append(ln)
        elif section == "skills":
            _parse_skill_line(ln, result["skills"])
        elif section == "experience":
            current = _parse_experience_line(ln, result["work_history"], current)
        elif section == "education":
            _parse_education_line(ln, result["education"])

    result["summary"] = " ".join(summary_parts).strip()
    return result


def _parse_skill_line(line: str, skills: dict) -> None:
    if ":" in line:
        category, _, items = line.partition(":")
        category = category.strip()
        items_list = [i.strip() for i in items.split(",") if i.strip()]
        if category and items_list:
            bucket = skills.setdefault(category, [])
            for item in items_list:
                if item not in bucket:
                    bucket.append(item)
            return
    items = [i.strip() for i in line.split(",") if i.strip()]
    if items:
        bucket = skills.setdefault("Skills", [])
        for item in items:
            if item not in bucket:
                bucket.append(item)


def _parse_experience_line(line: str, history: list[dict], current: dict | None) -> dict | None:
    m = DATE_RANGE_RE.search(line)
    if m:
        rest = (line[: m.start()] + " " + line[m.end():]).strip()
        rest = re.sub(r"\s{2,}", " ", rest)
        if rest and "," in rest and len(rest.partition(",")[0]) < 80:
            company_part, _, location = rest.partition(",")
            entry = {
                "company": company_part.strip(),
                "location": location.strip(),
                "title": "",
                "start": m.group(1).strip(),
                "end": m.group(2).strip(),
                "is_current": bool(re.search(r"present|current|now|today", m.group(2), re.IGNORECASE)),
                "project": "",
                "bullets": [],
            }
            history.append(entry)
            return entry
        if current is not None and not current["start"]:
            current["start"] = m.group(1).strip()
            current["end"] = m.group(2).strip()
            current["is_current"] = bool(re.search(r"present|current|now|today", m.group(2), re.IGNORECASE))
        return current

    if current is None:
        return None
    if PROJECT_RE.match(line):
        current["project"] = PROJECT_RE.sub("", line).strip()
        return current
    if not current["title"] and len(line) <= 60:
        current["title"] = line
        return current
    line = line.lstrip("-•· ").strip()
    if line:
        current["bullets"].append(line)
    return current


def _parse_education_line(line: str, education: list[dict]) -> None:
    if not DEGREE_RE.search(line):
        return
    left, sep, tail = line.partition("—")
    if not sep:
        left, sep, tail = line.partition("–")
    if not sep:
        left, sep, tail = line.partition("|")
    dates = ""
    if "|" in tail:
        tail, _, dates = tail.partition("|")
    degree_part = left.strip()
    if "," in degree_part:
        degree, _, field = degree_part.partition(",")
        degree, field = degree.strip(), field.strip()
    else:
        degree, field = degree_part, ""
    entry = {"degree": degree, "field": field, "school": tail.strip(), "start": "", "end": ""}
    m = DATE_RANGE_RE.search(dates or line)
    if m:
        entry["start"] = m.group(1).strip()
        entry["end"] = m.group(2).strip()
    education.append(entry)
