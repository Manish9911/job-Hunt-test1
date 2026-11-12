"""Render a structured resume to DOCX/PDF in the master resume's own format.

User rule (2026-11-05): tailored resumes must keep the MASTER resume's
structure and format — section order (Professional Summary → Technical
Skills → Professional Experience → Education), bold headings with trailing
colons, right-padded dates, real bullet lists, 10.5pt education lines.
Only the CONTENT is tailored per JD (reorder/reword/emphasize); the layout
follows the master. ATS-safe by construction: single column, plain
paragraphs, real bullet lists — no tables, text boxes, or columns.

DOCX: if the master docx is available (``template_path``) it is used as the
OOXML template — the generated file inherits the master's exact styles
(docDefaults font/spacing, bullet numbering, theme fonts) and every line
copies the formatting of a prototype paragraph from the master. Without a
template, a clean document replicating the master's layout is built.
"""
from __future__ import annotations

import copy
from pathlib import Path

# Section headings exactly as written on the master resume (trailing colon).
H_SUMMARY = "Professional Summary:"
H_SKILLS = "Technical Skills:"
H_EXPERIENCE = "Professional Experience:"
H_EDUCATION = "Education:"

# Role lines that are actually mis-parsed labels (not job titles).
_NON_TITLES = {"responsibilities:", ""}


def humanized_filename(*parts: "str | None", ext: str = "docx", max_len: int = 90) -> str:
    """Build a human resume filename: '<user> <company> <role>.<ext>'.

    User rule (2026-11-06): resume files must be named
    "<user name> <company name> <role name>" (e.g.
    "Manish Badugu Atoms Senior Software Engineer Infrastructure Security
    Platform.docx") — no ``resume_123`` IDs, no underscores or other symbols.
    Only letters, digits and spaces survive; everything else becomes a space.
    """
    import re

    pieces = []
    for p in parts:
        if not p:
            continue
        t = re.sub(r"[^A-Za-z0-9 ]+", " ", str(p))
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            pieces.append(t)
    name = " ".join(pieces)
    if len(name) > max_len:
        name = name[:max_len].rsplit(" ", 1)[0].rstrip()
    return f"{name or 'resume'}.{ext}"


_DATE_PAD = 64  # spaces the master uses to push dates to the right margin


def _job_span(job: dict) -> str:
    span = f"{job.get('start', '')} – {job.get('end', '')}".strip(" –")
    return span


def _iter_lines(structure: dict):
    """Yield (kind, text) pairs in the master resume's exact order.

    kinds: name, title, contact, blank, heading, normal, skill,
           company, role, label, bullet, edu
    """
    if structure.get("name"):
        yield "name", structure["name"]
    if structure.get("title"):
        yield "title", structure["title"]
    contact = "  |  ".join(x for x in (structure.get("phone"), structure.get("email")) if x)
    if contact:
        yield "contact", contact

    if structure.get("summary"):
        yield "blank", ""
        yield "heading", H_SUMMARY
        yield "normal", structure["summary"]

    skills = structure.get("skills") or {}
    if skills:
        yield "blank", ""
        yield "heading", H_SKILLS
        for cat, items in skills.items():
            text = ", ".join(items) if isinstance(items, list) else str(items)
            yield "skill", f"{cat}: {text}"

    wh = structure.get("work_history") or []
    if wh:
        yield "blank", ""
        yield "heading", H_EXPERIENCE
        for job in wh:
            left = ", ".join(x for x in (job.get("company"), job.get("location")) if x)
            span = _job_span(job)
            if span:
                pad = chr(32) * max(1, _DATE_PAD - len(left))
                company_line = f"{left}{pad}{span}"
            else:
                company_line = left
            yield "company", company_line
            title = (job.get("title") or "").strip()
            if title and title.lower() not in _NON_TITLES:
                yield "role", title
            if job.get("project"):
                yield "label", f"Project Name: {job['project']}"
            if not job.get("project") or job.get("responsibilities_label"):
                yield "label", "Responsibilities:"
            for b in job.get("bullets") or []:
                yield "bullet", b

    edu = structure.get("education") or []
    if edu:
        yield "blank", ""
        yield "heading", H_EDUCATION
        for e in edu:
            degree = f"{e.get('degree', '')}, {e.get('field', '')}".strip(", ")
            span = _job_span(e)
            line = f"{degree} — {e.get('school', '')}".strip(" —")
            if span:
                line = f"{line}   |   {span}"
            yield "edu", line


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _collect_prototypes(document) -> dict:
    """Prototype paragraph formatting from a master-format docx.

    Returns {kind: {"pPr": elem|None, "rPr": elem|None}} for the four
    formatting classes the master uses: bold line (name/headings/skills/
    companies), normal line (summary body), bullet (List Paragraph),
    education line (10.5pt).
    """
    protos: dict = {}

    def grab(kind, p):
        if kind in protos:
            return
        ppr = p._p.find(f"{_W}pPr")
        rpr = None
        for r in p._p.findall(f"{_W}r"):
            rpr_el = r.find(f"{_W}rPr")
            if rpr_el is not None:
                rpr = rpr_el
            break
        protos[kind] = {"pPr": copy.deepcopy(ppr) if ppr is not None else None,
                        "rPr": copy.deepcopy(rpr) if rpr is not None else None}

    section = None
    for p in document.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        if "bold" not in protos:
            grab("bold", p)  # first non-empty paragraph = name line
        if text == H_SUMMARY:
            section = "summary"
            continue
        if section == "summary" and "normal" not in protos:
            grab("normal", p)
            section = None
            continue
        if text == H_SKILLS:
            section = "skills"
            continue
        if text == H_EXPERIENCE:
            section = "experience"
            continue
        if text == H_EDUCATION:
            section = "education"
            continue
        if section == "experience" and "bullet" not in protos and p.style.name == "List Paragraph":
            grab("bullet", p)
        if section == "education" and "edu" not in protos:
            grab("edu", p)
        if all(k in protos for k in ("bold", "normal", "bullet", "edu")):
            break
    return protos


def _add_line(doc, protos: dict, kind: str, text: str, bold: bool):
    from docx.shared import Pt

    p = doc.add_paragraph()
    proto = protos.get(kind) if protos else None
    if proto is not None:
        if proto["pPr"] is not None:
            p._p.insert(0, copy.deepcopy(proto["pPr"]))
        if text:
            r = p.add_run(text)
            if proto["rPr"] is not None:
                r._r.insert(0, copy.deepcopy(proto["rPr"]))
        return p
    # Fallback: replicate the master's formatting explicitly.
    if text:
        r = p.add_run(text)
        r.bold = bold
        if kind == "bullet":
            r.font.size = Pt(11)
        elif kind == "edu":
            r.font.size = Pt(10.5)
    return p


def export_docx(structure: dict, path: Path, template_path: Path | None = None) -> Path:
    from docx import Document

    template = None
    try:
        if template_path is not None:
            template = Path(template_path)
    except TypeError:
        template = None
    if template is not None and not template.is_file():
        template = None

    if template is not None:
        doc = Document(str(template))
        protos = _collect_prototypes(doc)
        body = doc.element.body
        for child in list(body):
            if child.tag.endswith("}sectPr"):
                continue  # keep page setup
            body.remove(child)
    else:
        doc = Document()
        style = doc.styles["Normal"]
        style.font.name = "Calibri"
        style.paragraph_format.line_spacing = 1.15
        protos = {}

    for kind, text in _iter_lines(structure):
        if kind == "bullet":
            p = _add_line(doc, protos, "bullet", text, bold=False)
            if not protos:
                p.style = doc.styles["List Bullet"]
        elif kind == "normal":
            _add_line(doc, protos, "normal", text, bold=False)
        elif kind == "blank":
            doc.add_paragraph()
        else:
            _add_line(doc, protos, "edu" if kind == "edu" else "bold", text, bold=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def master_template_path(db) -> Path | None:
    """On-disk path of the master resume docx (the format template), if any."""
    from ..db.models import Resume

    master = db.query(Resume).filter(Resume.is_master.is_(True)).first()
    if master and master.storage_path:
        p = Path(master.storage_path)
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------------------
# PDF (fpdf2) — mirrors the same section order and labels
# ---------------------------------------------------------------------------

# fpdf's built-in (non-TTF) fonts are latin-1 only; map common unicode
# punctuation to ASCII so resume text (dates like "Jan 2023 – Present",
# curly quotes, em dashes) never crashes the renderer.
_PDF_SUBS = {
    "\u2013": "-",   # en dash
    "\u2014": "-",   # em dash
    "\u2018": "'",   # left single quote
    "\u2019": "'",   # right single quote
    "\u201c": '"',   # left double quote
    "\u201d": '"',   # right double quote
    "\u2026": "...", # ellipsis
    "\u2192": "->",  # right arrow
    "\u00a0": " ",   # non-breaking space
    "\u2022": "-",   # bullet (not in latin-1; use a dash)
}


def _pdf_text(text: str) -> str:
    s = "".join(_PDF_SUBS.get(c, c) for c in text)
    try:
        return s.encode("latin-1").decode("latin-1")
    except UnicodeEncodeError:
        return s.encode("latin-1", "ignore").decode("latin-1")


def export_pdf(structure: dict, path: Path) -> Path:
    from fpdf import FPDF

    class PDF(FPDF):
        def footer(self):
            self.set_y(-15)
            self.set_font("Helvetica", size=8)
            self.set_text_color(120)
            self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    pdf = PDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_auto_page_break(True, margin=15)

    def emit(text: str, size: float = 9.5, bold: bool = False, gap: float = 1.5,
             color=None):
        pdf.set_font("Helvetica", "B" if bold else "", size)
        if color:
            pdf.set_text_color(*color)
        else:
            pdf.set_text_color(20, 20, 20)
        pdf.multi_cell(0, 4.6, _pdf_text(text or " "))
        pdf.ln(gap)

    if structure.get("name"):
        emit(structure["name"], size=17, bold=True, gap=0.5)
    if structure.get("title"):
        emit(structure["title"], size=11, bold=True, gap=0.5)
    contact = "  |  ".join(x for x in (structure.get("phone"), structure.get("email")) if x)
    if contact:
        emit(contact, size=9, color=(90, 90, 90), gap=2)

    def section(title: str):
        pdf.ln(1.5)
        emit(title, size=11.5, bold=True, gap=0.5)

    if structure.get("summary"):
        section(H_SUMMARY)
        emit(structure["summary"], size=9.5)

    skills = structure.get("skills") or {}
    if skills:
        section(H_SKILLS)
        for cat, items in skills.items():
            text = ", ".join(items) if isinstance(items, list) else str(items)
            pdf.set_font("Helvetica", "B", 9.5)
            pdf.multi_cell(0, 4.6, _pdf_text(f"**{cat}:** {text}"), markdown=True)
            pdf.ln(0.8)

    wh = structure.get("work_history") or []
    if wh:
        section(H_EXPERIENCE)
        for job in wh:
            left = _pdf_text(", ".join(x for x in (job.get("company"), job.get("location")) if x))
            span = _pdf_text(_job_span(job))
            pdf.set_font("Helvetica", "B", 10)
            avail = pdf.w - pdf.l_margin - pdf.r_margin
            if span:
                x0 = pdf.l_margin
                y0 = pdf.get_y()
                pdf.set_xy(x0, y0)
                pdf.cell(pdf.get_string_width(left), 5, left)
                pdf.set_xy(x0 + avail - pdf.get_string_width(span), y0)
                pdf.cell(pdf.get_string_width(span), 5, span,
                         new_x="LMARGIN", new_y="NEXT")
            else:
                emit(left, size=10, bold=True, gap=0.5)
            title = (job.get("title") or "").strip()
            if title and title.lower() not in _NON_TITLES:
                emit(title, size=10, bold=True, gap=0.5)
            if job.get("project"):
                emit(f"Project Name: {job['project']}", size=9.5, bold=True, gap=0.5)
            if not job.get("project") or job.get("responsibilities_label"):
                emit("Responsibilities:", size=9.5, bold=True, gap=0.5)
            for b in job.get("bullets") or []:
                pdf.set_x(pdf.l_margin + 4)
                emit("\u2022  " + b, size=9.5, gap=0.6)

    edu = structure.get("education") or []
    if edu:
        section(H_EDUCATION)
        for e in edu:
            degree = f"{e.get('degree', '')}, {e.get('field', '')}".strip(", ")
            span = _job_span(e)
            line = f"{degree} — {e.get('school', '')}".strip(" —")
            if span:
                line = f"{line}   |   {span}"
            emit(line, size=9.5, bold=True, gap=0.8)

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path
