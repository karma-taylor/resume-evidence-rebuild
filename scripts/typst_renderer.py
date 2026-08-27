#!/usr/bin/env python3
"""Render a validated resume plan into the authoritative one-page Typst PDF."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream) if path.suffix in {".yaml", ".yml"} else json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain an object")
    return data


def esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace("#", "\\#").replace("[", "\\[").replace("]", "\\]")


def rich_text(text: str, phrases: list[str]) -> str:
    result = esc(text)
    for phrase in sorted(phrases, key=len, reverse=True):
        result = result.replace(esc(phrase), f"#strong[{esc(phrase)}]")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--resume-plan", type=Path, required=True)
    parser.add_argument("--typeset-plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    profile, template, resume_plan, typeset = map(load, (args.profile, args.template, args.resume_plan, args.typeset_plan))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    identity = profile["identity"]
    by_project = {item["id"]: item for item in resume_plan["projects"]}
    blocks: list[str] = []
    manifest: list[dict[str, Any]] = []
    for project in typeset["projects"]:
        source = by_project[project["id"]]
        blocks.append(f"#text(size: 11pt, weight: \"semibold\")[{esc(source['title'])}] #h(1fr) #text(size: 9pt)[{esc(source['start'])} – {esc(source['end'])}]")
        manifest_bullets: list[dict[str, Any]] = []
        for number, bullet in enumerate(project["bullets"], 1):
            element_id = f"project.{project['id']}.bullet.{number}"
            blocks.append(f"#metadata(\"{element_id}\") #par(first-line-indent: 0pt, hanging-indent: 10pt)[• {rich_text(bullet['text'], bullet['bold_phrases_used'])}]")
            manifest_bullets.append({"element_id": element_id, "text": bullet["text"], "bold_phrases": bullet["bold_phrases_used"], "source_claim_ids": bullet["source_claim_ids"]})
        manifest.append({"id": project["id"], "name": source["title"], "bullets": manifest_bullets})
    employment_blocks = []
    for item in profile.get("employment", []):
        employment_blocks.append(
            f"#text(size: 10pt, weight: \"semibold\")[{esc(item.get('employer', ''))} | {esc(item.get('title', ''))}] "
            f"#h(1fr) #text(size: 9pt)[{esc(item.get('start', ''))} – {esc(item.get('end', ''))}]\n"
            f"#text(size: 9pt)[{esc(item.get('summary', ''))}]"
        )
    certs = " · ".join(profile.get("certifications", []))
    education = " · ".join(f"{item.get('school', '')} {item.get('degree', '')}" for item in profile.get("education", []))
    contact = json.dumps("  |  ".join(str(identity[key]) for key in ("phone", "email", "portfolio_url")), ensure_ascii=False)
    header = f"#text(size: 19pt, weight: \"bold\")[{esc(identity['name'])}]\n#text(size: 11pt, weight: \"semibold\")[{esc(template['target_role'])}]\n#text(size: 9pt)[#raw({contact})]"
    typst = """#set page(paper: \"a4\", margin: (top: 1.7cm, bottom: 1.7cm, left: 1.7cm, right: 1.7cm))
#set text(font: (\"Heiti SC\", \"Arial Unicode MS\", \"Arial\"), size: 10pt, fill: rgb(18, 18, 18))
#set par(leading: 14pt)
#set heading(numbering: none)
#align(left)[
""" + header + """

#v(8pt)
#line(length: 100%, stroke: rgb(33, 95, 154) + 0.9pt)
#v(6pt)
#text(size: 12pt, weight: \"bold\")[Work Experience]
#v(3pt)
""" + "\n#v(4pt)\n".join(employment_blocks) + """
#v(7pt)
#text(size: 12pt, weight: \"bold\")[Projects]
#v(3pt)
""" + "\n#v(9pt)\n".join(blocks) + """
#v(6pt)
#text(size: 11pt, weight: \"bold\")[Education & Certifications]
#v(2pt)
#text(size: 9pt)[""" + esc(education) + """ ]
#v(2pt)
#strong[#text(size: 9pt)[""" + esc(certs) + """]]
]
"""
    typst_path, pdf_path = args.output_dir / "resume.typ", args.output_dir / "resume.pdf"
    typst_path.write_text(typst, encoding="utf-8")
    binary = shutil.which("typst") or "/Users/taylorkarma/.local/bin/typst"
    subprocess.run([binary, "compile", str(typst_path), str(pdf_path)], check=True)
    (args.output_dir / "project-manifest.json").write_text(json.dumps({"projects": manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"pdf": str(pdf_path), "project_manifest": str(args.output_dir / "project-manifest.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
