#!/usr/bin/env python3
"""Optional editable DOCX renderer; Typst remains the PDF layout authority."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml
from docx import Document
from docx.shared import Cm, Pt


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream) if path.suffix in {".yaml", ".yml"} else json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def add_rich(paragraph: Any, text: str, phrases: list[str], size: float = 10) -> None:
    cursor = 0
    ordered = sorted(phrases, key=lambda item: text.find(item))
    for phrase in ordered:
        start = text.find(phrase, cursor)
        if start < 0:
            raise ValueError(f"declared bold phrase {phrase!r} is absent")
        if start > cursor:
            paragraph.add_run(text[cursor:start]).font.size = Pt(size)
        run = paragraph.add_run(phrase)
        run.bold, run.font.size = True, Pt(size)
        cursor = start + len(phrase)
    if cursor < len(text):
        paragraph.add_run(text[cursor:]).font.size = Pt(size)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--resume-plan", type=Path, required=True)
    parser.add_argument("--typeset-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile, template, plan, typeset = map(load, (args.profile, args.template, args.resume_plan, args.typeset_plan))
    doc = Document()
    section = doc.sections[0]
    section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Cm(1.7)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Heiti SC", Pt(10)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(8)
    identity = profile["identity"]
    title = doc.add_paragraph(); title.add_run(identity["name"]).bold = True; title.runs[0].font.size = Pt(18)
    doc.add_paragraph(template["target_role"])
    doc.add_paragraph(" | ".join(str(identity[key]) for key in ("phone", "email", "portfolio_url")))
    work = doc.add_paragraph(); work.add_run("Work Experience").bold = True
    for item in profile.get("employment", []):
        para = doc.add_paragraph(); para.add_run(f"{item.get('employer', '')} | {item.get('title', '')} | {item.get('start', '')} – {item.get('end', '')}").bold = True
        doc.add_paragraph(item.get("summary", ""))
    projects = {item["id"]: item for item in plan["projects"]}
    heading = doc.add_paragraph(); heading.add_run("Projects").bold = True
    manifest = []
    for project in typeset["projects"]:
        source = projects[project["id"]]
        para = doc.add_paragraph(); para.add_run(f"{source['title']} | {source['start']} – {source['end']}").bold = True
        bullets = []
        for number, bullet in enumerate(project["bullets"], 1):
            para = doc.add_paragraph(style="List Bullet")
            add_rich(para, bullet["text"], bullet["bold_phrases_used"])
            bullets.append({"element_id": f"project.{project['id']}.bullet.{number}", "text": bullet["text"], "bold_phrases": bullet["bold_phrases_used"], "source_claim_ids": bullet["source_claim_ids"]})
        manifest.append({"id": project["id"], "name": source["title"], "bullets": bullets})
    edu = doc.add_paragraph(); edu.add_run("Education & Certifications").bold = True
    doc.add_paragraph(" · ".join(f"{entry.get('school', '')} {entry.get('degree', '')}" for entry in profile.get("education", [])))
    certs = doc.add_paragraph()
    for index, cert in enumerate(profile.get("certifications", [])):
        if index: certs.add_run(" · ")
        run = certs.add_run(cert); run.bold = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(args.output)
    print(json.dumps({"docx": str(args.output), "project_manifest": manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
