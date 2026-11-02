"""Render a structured resume to DOCX and PDF (deterministic, no external tools).

DOCX via python-docx; PDF via fpdf2. Both are generated from the same
structured dict the parser and the tailoring engine use, so exports always
match what the dashboard shows.
"""
from __future__ import annotations

from pathlib import Path


def export_docx(structure: dict, path: Path) -> Path:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.size = Pt(10.5)

    if structure.get("name"):
        h = doc.add_heading(structure["name"], level=0)
        for run in h.runs:
            run.font.size = Pt(20)
    contact = "  |  ".join(x for x in (structure.get("email"), structure.get("phone")) if x)
    if contact:
        p = doc.add_paragraph(contact)
        p.runs[0].font.size = Pt(9)

    def section(title: str):
        doc.add_heading(title, level=2)

    if structure.get("summary"):
        section("Summary")
        doc.add_paragraph(structure["summary"])

    skills = structure.get("skills") or {}
    if skills:
        section("Skills")
        for cat, items in skills.items():
            text = ", ".join(items) if isinstance(items, list) else str(items)
            doc.add_paragraph(f"{cat}: {text}")

    wh = structure.get("work_history") or []
    if wh:
        section("Experience")
        for job in wh:
            span = f"{job.get('start', '')} - {job.get('end', '')}".strip(" -")
            line = f"{job.get('title', '')}  |  {job.get('company', '')}"
            extra = f"  |  {job.get('location', '')}" if job.get("location") else ""
            doc.add_heading(line + extra + (f"  ({span})" if span else ""), level=3)
            if job.get("project"):
                p = doc.add_paragraph(job["project"])
                p.runs[0].italic = True
            for b in job.get("bullets") or []:
                doc.add_paragraph(b, style="List Bullet")

    edu = structure.get("education") or []
    if edu:
        section("Education")
        for e in edu:
            span = f"{e.get('start', '')} - {e.get('end', '')}".strip(" -")
            degree = f"{e.get('degree', '')} {e.get('field', '')}".strip()
            doc.add_paragraph(
                f"{degree}, {e.get('school', '')}" + (f"  ({span})" if span else ""),
                style="List Bullet",
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def export_pdf(structure: dict, path: Path) -> Path:
    from fpdf import FPDF

    class PDF(FPDF):
        def header(self):
            pass

        def footer(self):
            self.set_y(-15)
            self.set_font("Helvetica", size=8)
            self.set_text_color(120)
            self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    pdf = PDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_auto_page_break(True, margin=15)

    def emit(text: str, font: str = "Helvetica", size: float = 10, bold: bool = False,
             gap: float = 1.5, color=None):
        pdf.set_font(font, "B" if bold else "", size)
        if color:
            pdf.set_text_color(*color)
        else:
            pdf.set_text_color(20, 20, 20)
        pdf.multi_cell(0, 5, text or " ")
        pdf.ln(gap)

    if structure.get("name"):
        emit(structure["name"], size=18, bold=True, gap=1)
    contact = "  |  ".join(x for x in (structure.get("email"), structure.get("phone")) if x)
    if contact:
        emit(contact, size=9, color=(90, 90, 90), gap=3)

    def section(title: str):
        pdf.ln(1)
        emit(title, size=12, bold=True, gap=1)
        pdf.set_draw_color(60, 100, 200)
        y = pdf.get_y()
        pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
        pdf.ln(1)

    if structure.get("summary"):
        section("Summary")
        emit(structure["summary"], size=9.5)

    skills = structure.get("skills") or {}
    if skills:
        section("Skills")
        for cat, items in skills.items():
            text = ", ".join(items) if isinstance(items, list) else str(items)
            emit(f"{cat}: {text}", size=9.5, gap=0.8)

    wh = structure.get("work_history") or []
    if wh:
        section("Experience")
        for job in wh:
            span = f"{job.get('start', '')} - {job.get('end', '')}".strip(" -")
            line = f"{job.get('title', '')} | {job.get('company', '')}"
            if job.get("location"):
                line += f" | {job['location']}"
            if span:
                line += f" ({span})"
            emit(line, size=10.5, bold=True, gap=0.5)
            if job.get("project"):
                emit(job["project"], size=9, gap=0.5)
            for b in job.get("bullets") or []:
                pdf.set_x(pdf.l_margin + 4)
                emit("- " + b, size=9.5, gap=0.6)

    edu = structure.get("education") or []
    if edu:
        section("Education")
        for e in edu:
            span = f"{e.get('start', '')} - {e.get('end', '')}".strip(" -")
            degree = f"{e.get('degree', '')} {e.get('field', '')}".strip()
            emit(f"- {degree}, {e.get('school', '')}" + (f" ({span})" if span else ""), size=9.5, gap=0.8)

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path
