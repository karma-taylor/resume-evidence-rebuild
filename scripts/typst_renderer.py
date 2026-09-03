#!/usr/bin/env python3
"""Render a validated resume plan into the authoritative one-page Typst PDF."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from design_tokens import load_theme
from validate_resume_artifacts import atomic_write_json, ResumeQAError, quarantine_artifacts, run_pdf_delivery_gate, write_delivery_manifest
from build_resume import load_and_validate_render_inputs

FROZEN_LAYOUTS = {
    # Paragraph spacing is deliberately kept outside this reflow budget.  It
    # is a fixed 5pt between bullets; changing it per round makes the same
    # content look erratic and was the source of the previous airy layout.
    "normal": {"header_to_first_module": 10.0, "module_gap": 7.0, "project_gap": 8.0, "title_to_overview": 4.0, "overview_to_bullet": 3.0},
    "compact_1": {"header_to_first_module": 8.0, "module_gap": 5.0, "project_gap": 6.0, "title_to_overview": 3.0, "overview_to_bullet": 2.0},
    "compact_2": {"header_to_first_module": 6.0, "module_gap": 4.0, "project_gap": 5.0, "title_to_overview": 2.0, "overview_to_bullet": 1.0},
    "sparse_fill": {"header_to_first_module": 14.0, "module_gap": 25.0, "project_gap": 40.0, "title_to_overview": 4.0, "overview_to_bullet": 3.0},
    "sparse_fill_compact": {"header_to_first_module": 14.0, "module_gap": 15.0, "project_gap": 12.0, "title_to_overview": 4.0, "overview_to_bullet": 3.0},
    "sparse_fill_tight": {"header_to_first_module": 6.0, "module_gap": 4.0, "project_gap": 5.0, "title_to_overview": 2.0, "overview_to_bullet": 1.0},
    "compact_3": {"header_to_first_module": 4.0, "module_gap": 2.0, "project_gap": 2.0, "title_to_overview": 1.0, "overview_to_bullet": 0.5},
    "compact_4": {"header_to_first_module": 2.0, "module_gap": 0.0, "project_gap": 0.0, "title_to_overview": 0.0, "overview_to_bullet": 0.0},
}
CONTENT_BOUNDS = {
    "normal": (40, 50),
    "compressed": (30, 40),
    "expanded": (50, 130),
}


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream) if path.suffix in {".yaml", ".yml"} else json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain an object")
    return data


def esc(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("#", "\\#")
            .replace("@", "\\@").replace("[", "\\[").replace("]", "\\]")
            .replace("-", "\\-"))


def rich_text(text: str, phrases: list[str]) -> str:
    result = esc(text)
    for phrase in sorted(phrases, key=len, reverse=True):
        # The local Microsoft YaHei installation may contain only the Regular
        # face.  Keep the requested bold weight and add a very light stroke so
        # important evidence remains visibly bold instead of silently falling
        # back to indistinguishable regular glyphs.
        result = result.replace(
            esc(phrase),
            f'#text(weight: "bold", fill: rgb("#000000"), stroke: 0.12pt + rgb("#000000"))[{esc(phrase)}]',
        )
    return result


def business_display_text(bullet: dict[str, Any], *, include_background: bool = True) -> str:
    """Build the reader-facing three-stage sentence without changing facts."""
    structure = bullet.get("business_structure") or {}
    if not all(isinstance(structure.get(key), dict) for key in ("business_difficulty", "solution_action", "quantified_result")):
        return str(bullet["text"])
    stages = (("背景", "business_difficulty"), ("解决", "solution_action"), ("结果", "quantified_result"))
    if not include_background:
        stages = stages[1:]
    return "；".join(f"{label}：{structure[key].get('text', '')}" for label, key in stages)


def business_rich_text(bullet: dict[str, Any], *, include_background: bool = True) -> str:
    """Render business labels as bold anchors and preserve terminal emphasis."""
    structure = bullet.get("business_structure") or {}
    if not all(isinstance(structure.get(key), dict) for key in ("business_difficulty", "solution_action", "quantified_result")):
        return rich_text(str(bullet["text"]), list(bullet.get("bold_phrases_used", [])))
    phrases = [str(item) for item in bullet.get("bold_phrases_used", [])]
    pieces: list[str] = []
    stages = (("背景", "business_difficulty"), ("解决", "solution_action"), ("结果", "quantified_result"))
    if not include_background:
        stages = stages[1:]
    for index, (label, key) in enumerate(stages):
        if index:
            pieces.append("；")
        label_markup = f'#text(weight: "bold", fill: rgb("#000000"), stroke: 0.12pt + rgb("#000000"))[{label}：]'
        segment_text = str(structure[key].get("text", ""))
        segment_phrases = [phrase for phrase in phrases if phrase in segment_text]
        pieces.extend((label_markup, rich_text(segment_text, segment_phrases)))
    return "".join(pieces)


STAGE_LABELS = {"background": "背景", "solution": "解决", "result": "结果"}


def stage_display_text(bullet: dict[str, Any]) -> str:
    label = STAGE_LABELS[str(bullet.get("stage"))]
    return f"{label}：{bullet['text']}"


def stage_rich_text(bullet: dict[str, Any]) -> str:
    label = STAGE_LABELS[str(bullet.get("stage"))]
    label_markup = f'#text(weight: "bold", fill: rgb("#000000"), stroke: 0.12pt + rgb("#000000"))[{label}：]'
    return label_markup + rich_text(str(bullet["text"]), list(bullet.get("bold_phrases_used", [])))


def visible_bold(value: str, size: float) -> str:
    """Render bold visibly even when Microsoft YaHei only has Regular installed."""
    return (
        f'#text(size: {size}pt, weight: "bold", '
        f'fill: design-color("accent"), stroke: 0.12pt + design-color("accent"))[{esc(value)}]'
    )


def contains_cjk(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff" for char in text)


def cjk_count(text: str) -> int:
    return sum("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff" for char in text)


def typst_path(path: Path) -> str:
    """Quote an image path for Typst without interpreting it as markup."""
    return json.dumps(str(path.resolve()), ensure_ascii=False)


def section_heading(label: str) -> str:
    """Return a compact section title with a restrained horizontal divider."""
    return (
        '#grid(columns: (auto, 1fr), gutter: 5pt, '
        '[#rect(width: design-len("section_marker_width_pt"), height: design-len("section_marker_height_pt"), fill: design-color("accent"))], '
        f'[{visible_bold(label, 12)}])\n'
        '#v(0pt)\n#line(length: 100%, stroke: design-color("rule") + design-len("section_rule_pt"))\n#v(5pt)'
    )


def _main_impl() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--resume-plan", type=Path, required=True)
    parser.add_argument("--typeset-plan", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, help="Approved ingestion inbox required when employment exists")
    parser.add_argument("--jd-brief", type=Path, help="Structured JD paired with --jd-evidence-map for dynamic selection")
    parser.add_argument("--jd-evidence-map", type=Path, help="Current local-source mapping paired with --jd-brief")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--layout-vars", type=Path, help="Dynamic layout JSON; defaults to output-dir/layout_vars.json")
    parser.add_argument("--theme-vars", "--design-tokens", dest="theme_vars", type=Path, required=True, help="Frozen allow-listed theme_vars.json")
    parser.add_argument("--internal-reflow", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    cjk_font = os.environ.get("RESUME_CJK_FONT", "Microsoft YaHei")
    # The renderer is also a public entry point.  It therefore cannot trust
    # caller-provided JSON just because build_resume.py usually produced it.
    # Admission happens before a single string is copied into Typst.
    validated_profile, validated_template, validated_resume_plan, validated_typeset = load_and_validate_render_inputs(
        profile_path=args.profile,
        template_path=args.template,
        resume_plan_path=args.resume_plan,
        typeset_plan_path=args.typeset_plan,
        inbox_path=args.inbox,
        jd_brief_path=args.jd_brief,
        jd_evidence_map_path=args.jd_evidence_map,
        allow_recovery_subset=args.internal_reflow,
    )
    # Render from the validated in-memory values, rather than re-reading four
    # attacker-controlled files after validation (a TOCTOU bypass).
    profile = validated_profile.model_dump(mode="json")
    template = validated_template.model_dump(mode="json")
    resume_plan = validated_resume_plan.model_dump(mode="json")
    typeset = validated_typeset.model_dump(mode="json")
    content_mode = str(typeset.get("content_mode", "normal"))
    min_content_chars, max_content_chars = CONTENT_BOUNDS[content_mode]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    theme = load_theme(args.theme_vars)
    if args.theme_vars.resolve() != (args.output_dir / "theme_vars.json").resolve():
        shutil.copy2(args.theme_vars, args.output_dir / "theme_vars.json")
    layout_vars_path = args.layout_vars or args.output_dir / "layout_vars.json"
    if not layout_vars_path.is_file():
        raise ValueError(f"layout_vars.json is required for Typst rendering: {layout_vars_path}")
    layout_vars = load(layout_vars_path)
    if layout_vars.get("layout_state") not in FROZEN_LAYOUTS:
        raise ValueError("layout_vars.json has an unsupported layout_state")
    required_spacing = {"header_to_first_module", "module_gap", "project_gap", "title_to_overview", "overview_to_bullet"}
    spacing = layout_vars.get("spacing")
    if not isinstance(spacing, dict) or set(spacing) != required_spacing or spacing != FROZEN_LAYOUTS[layout_vars["layout_state"]]:
        raise ValueError("LAYOUT_VARS_TAMPERING_ERROR: spacing must exactly match its frozen layout_state")
    if layout_vars_path.resolve() != (args.output_dir / "layout_vars.json").resolve():
        shutil.copy2(layout_vars_path, args.output_dir / "layout_vars.json")
    identity = profile["identity"]
    # Typst leading is inter-line whitespace rather than Word's multiplier.
    # Explicit text edges plus 0.4em leading produce a 1.4em
    # baseline-to-baseline rhythm at each regular body text size, including CJK.
    # Bullet blocks scope 0.3em leading locally for 1.3x wrapped-line rhythm;
    # their fixed 5pt paragraph gap produces a 1.5x separation between bullets.
    line_height_multiplier = float(template.get("layout", {}).get("body_line_height_multiplier", 1.4))
    if line_height_multiplier != 1.4:
        raise ValueError("template layout.body_line_height_multiplier must be 1.4")
    chinese = contains_cjk(template["target_role"])
    labels = ("技术能力", "工作经历", "项目经历", "教育与证书") if chinese else ("Technical Skills", "Work Experience", "Projects", "Education & Certifications")
    by_project = {item["id"]: item for item in resume_plan["projects"]}
    blocks: list[str] = []
    manifest: list[dict[str, Any]] = []
    for project in typeset["projects"]:
        source = by_project[project["id"]]
        stage_mode = bool(project["bullets"]) and all(isinstance(bullet, dict) and bullet.get("stage") for bullet in project["bullets"])
        project_parts = [
            '#block(width: 100%)[',
            '#grid(columns: (1fr, auto), gutter: 8pt, '
            f'[{visible_bold(source["title"], 11)}], '
            f'[#text(size: 10pt, fill: rgb("#000000"))[{esc(source["start"])} – {esc(source["end"])}]],',
            ')',
            '#v(0pt)',
            '#line(length: 28pt, stroke: design-color("accent") + 0.4pt)',
        ]
        overview_data = project.get("overview") or {}
        overview = str(overview_data.get("text") or "").strip()
        if overview and not stage_mode:
            overview_id = f"project.{project['id']}.overview"
            overview_display = f"背景：{overview}"
            project_parts.append(f'#v(layout-len("title_to_overview"))\n#metadata("{overview_id}") #text(size: 9pt, fill: rgb("#000000"))[{esc(overview_display)}]\n#v(layout-len("overview_to_bullet"))')
        elif stage_mode:
            # Stage-form projects carry their business background in the first
            # bullet rather than a separate overview node. Reuse the frozen
            # title-to-overview spacing as the visible title-to-body gap. Keep
            # the overview-to-bullet key in the source as an audit reference;
            # there is no overview node in this format, so it contributes no
            # additional vertical space.
            project_parts.append(
                '#v(layout-len("title_to_overview"))\n'
                '#v(layout-len("overview_to_bullet") * 0)'
            )
        manifest_bullets: list[dict[str, Any]] = []
        for number, bullet in enumerate(project["bullets"], 1):
            element_id = f"project.{project['id']}.bullet.{number}"
            display_text = stage_display_text(bullet) if stage_mode else business_display_text(bullet, include_background=False)
            bullet_markup = stage_rich_text(bullet) if stage_mode else business_rich_text(bullet, include_background=False)
            project_parts.append(f"#metadata(\"{element_id}\") #bullet[#par(first-line-indent: 0pt, hanging-indent: 10pt)[• {bullet_markup}]]")
            derived_metric = bullet.get("derived_metric") if stage_mode else ((bullet.get("business_structure") or {}).get("quantified_result") or {}).get("derived_metric")
            manifest_bullets.append({"element_id": element_id, "text": display_text, "source_text": bullet["text"], "bold_phrases": bullet["bold_phrases_used"], "source_claim_ids": bullet["source_claim_ids"], "derived_metric": derived_metric})
        project_parts.append(']')
        blocks.append("\n".join(project_parts))
        manifest.append({"id": project["id"], "name": source["title"], "overview": (f"背景：{overview}" if overview else None), "overview_source_claim_ids": overview_data.get("source_claim_ids", []), "bullets": manifest_bullets})
    typeset_employment = {
        str(item.get("id")): item
        for item in typeset.get("employment", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    expected_employment_ids = {f"employment-{index}" for index, _ in enumerate(profile.get("employment", []), 1)}
    if set(typeset_employment) != expected_employment_ids:
        raise ValueError("typeset plan must contain exactly one approved work-bullet set per employment entry")
    employment_blocks: list[str] = []
    employment_manifest: list[dict[str, Any]] = []
    for index, item in enumerate(profile.get("employment", []), 1):
        employment_id = f"employment-{index}"
        work_bullets = typeset_employment[employment_id].get("bullets", [])
        if not isinstance(work_bullets, list) or not 4 <= len(work_bullets) <= 5:
            raise ValueError("each employment entry must contain 4-5 validated business bullets")
        if content_mode == "compressed":
            work_min_chars, work_max_chars = CONTENT_BOUNDS["compressed"]
        elif content_mode == "expanded":
            work_min_chars, work_max_chars = 40, CONTENT_BOUNDS["expanded"][1]
        else:
            work_min_chars, work_max_chars = CONTENT_BOUNDS["normal"]
        if (any(not isinstance(detail, dict) for detail in work_bullets)
                or any(not work_min_chars <= cjk_count(str(detail.get("text", ""))) <= work_max_chars for detail in work_bullets)):
            raise ValueError(f"each employment bullet must contain {work_min_chars}-{work_max_chars} CJK characters")
        work_heading = f"{item.get('employer', '')} | {item.get('title', '')}"
        detail_blocks = "\n".join(
                    f'#metadata("{employment_id}.bullet.{number}") #bullet[#par(first-line-indent: 0pt, hanging-indent: 10pt)[• {rich_text(str(detail["text"]), list(detail.get("bold_phrases_used", [])))}]]'
            for number, detail in enumerate(work_bullets, 1)
        )
        employment_manifest.append({
            "id": employment_id,
            "name": work_heading,
            "bullets": [
                {
                    "element_id": f"{employment_id}.bullet.{number}",
                    "text": str(detail["text"]),
                    "source_text": detail["text"],
                    "bold_phrases": detail["bold_phrases_used"],
                    "source_ingestion_ids": detail["source_ingestion_ids"],
                    "derived_metric": (((detail.get("business_structure") or {}).get("quantified_result") or {}).get("derived_metric")),
                }
                for number, detail in enumerate(work_bullets, 1)
            ],
        })
        employment_blocks.append(
            '#block(width: 100%)[\n'
            '#grid(columns: (1fr, auto), gutter: 10pt, \n'
            f'[{visible_bold(work_heading, 10)}], \n'
            f'[#text(size: 10pt, fill: rgb("#000000"))[{esc(item.get("start", ""))} – {esc(item.get("end", ""))}]], \n'
            ')\n#v(1pt)\n'
            f'{detail_blocks}\n]'
        )
    certs = " · ".join(profile.get("certifications", []))
    education = " · ".join(
        f"{item.get('school', '')} {item.get('degree', '')} {item.get('start', '')}–{item.get('end', '')}".strip()
        for item in profile.get("education", [])
    )
    phone_label = "电话" if chinese else "Phone"
    email_label = "邮箱" if chinese else "Email"
    location_label = "地点" if chinese else "Location"
    portfolio_label = "作品集" if chinese else "Portfolio"
    contact_line_one = json.dumps(
        f"{phone_label}：{identity['phone']} | {email_label}：{identity['email']}", ensure_ascii=False,
    )
    line_two_parts: list[str] = []
    if str(identity.get("location") or "").strip():
        line_two_parts.append(f"{location_label}：{identity['location']}")
    if str(identity.get("portfolio_url") or "").strip():
        line_two_parts.append(f"{portfolio_label}：{identity['portfolio_url']}")
    contact_line_two = json.dumps(" | ".join(line_two_parts), ensure_ascii=False)
    contact_markup = (
        f'#stack(dir: ttb, spacing: 7pt, '
        f'[#text(size: 10pt)[#raw({contact_line_one})]], '
        f'[#text(size: 10pt)[#raw({contact_line_two})]])'
        if line_two_parts
        else f'#text(size: 10pt)[#raw({contact_line_one})]'
    )
    header_text = (
        f"{visible_bold(identity['name'], 20)} #h(8pt) "
        f"#text(size: 11.5pt, weight: \"semibold\", fill: design-color(\"accent\"))[{esc(template['target_role'])}]\n"
        f"#v(20pt)\n{contact_markup}"
    )
    # ``technical_skills`` is the explicit field; ``summary`` remains a
    # backwards-compatible alias. Both render as one compact paragraph.
    summary = str(template.get("technical_skills") or template.get("summary", "")).strip()
    summary_bold_phrases = [str(item) for item in template.get("summary_bold_phrases", [])]
    summary_block = ""
    if summary:
        summary_block = (
            section_heading(labels[0])
            + "\n#par(first-line-indent: 0pt, hanging-indent: 0pt)["
            + rich_text(summary, summary_bold_phrases)
            + "]\n"
        )
    market = str(template.get("market", identity.get("market", ""))).upper()
    raw_photo_path = identity.get("photo_path")
    photo_path = raw_photo_path.strip() if isinstance(raw_photo_path, str) else ""
    if market == "CN" and photo_path:
        photo = Path(photo_path).expanduser()
        if not photo.is_absolute():
            photo = args.profile.parent / photo
        if not photo.is_file():
            raise ValueError(f"CN resume photo does not exist: {photo}")
        rendered_photo = args.output_dir / f"resume-photo{photo.suffix.lower()}"
        shutil.copy2(photo, rendered_photo)
        header = (
            '#grid(columns: (1fr, 2.15cm), gutter: 12pt, \n'
            f'[{header_text}], \n'
            f'[#align(right)[#move(dx: -8pt)[#image({json.dumps(rendered_photo.name)}, width: 2.15cm, height: 2.87cm, fit: "cover")]]]\n)'
        )
    else:
        header = header_text
    typst = """#let layout = json("layout_vars.json")
#let design = json("theme_vars.json")
#let layout-len(key) = layout.at("spacing").at(key) * 1pt
#let design-len(key) = design.at("tokens").at("lines").at(key) * 1pt
#let design-color(key) = rgb(if key == "date_color" or key == "overview_color" { design.at("tokens").at("hierarchy").at(key) } else { design.at("tokens").at("palette").at(key) })
// Keep the ink boundary above the 36pt QA floor across both Microsoft YaHei
// and the redistributable Noto CJK smoke-test font.  The page margin remains
// comfortably inside the documented 1.27–2.54cm contract.
#set page(paper: \"a4\", margin: (top: 1.35cm, bottom: 1.27cm, left: 1.7cm, right: 1.7cm))
#set text(font: """ + json.dumps(cjk_font, ensure_ascii=False) + """, size: 10pt, fill: rgb("#000000"))
#set text(top-edge: 0.8em, bottom-edge: -0.2em)
#set par(leading: 0.4em, spacing: 0.5pt)
#let bullet(body) = {
  set par(leading: 0.3em, spacing: 5pt)
  body
}
#set heading(numbering: none)
#align(left)[
""" + header + """

#v(4pt)
#line(length: 100%, stroke: design-color("accent") + design-len("header_rule_pt"))
#v(layout-len("header_to_first_module"))
""" + summary_block + """
#v(layout-len("module_gap"))
""" + section_heading(labels[1]) + """
""" + "\n#v(layout-len(\"module_gap\"))\n".join(employment_blocks) + """
#v(layout-len("module_gap"))
""" + section_heading(labels[2]) + """
""" + "\n#v(layout-len(\"project_gap\"))\n".join(blocks) + """
#v(layout-len("module_gap"))
""" + section_heading(labels[3]) + """
#text(size: 10pt)[""" + esc(education) + """ ]
""" + rich_text(certs, []) + """
]
"""
    typst_source_path, pdf_path = args.output_dir / "resume.typ", args.output_dir / "resume.pdf"
    typst_source_path.write_text(typst, encoding="utf-8")
    binary = shutil.which("typst")
    if not binary:
        raise RuntimeError("typst executable not found on PATH")
    subprocess.run([binary, "compile", str(typst_source_path), str(pdf_path)], check=True)
    project_manifest_path = args.output_dir / "project-manifest.json"
    atomic_write_json(project_manifest_path, {
        "projects": manifest,
        "employment": employment_manifest,
        "content_mode": content_mode,
        "layout_state": layout_vars["layout_state"],
        "theme_variant": theme["variant_id"],
        "provenance": {
            "renderer": "canonical_typst_renderer",
            "renderer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "profile_sha256": hashlib.sha256(args.profile.read_bytes()).hexdigest(),
            "template_sha256": hashlib.sha256(args.template.read_bytes()).hexdigest(),
            "resume_plan_sha256": hashlib.sha256(args.resume_plan.read_bytes()).hexdigest(),
            "typeset_plan_sha256": hashlib.sha256(args.typeset_plan.read_bytes()).hexdigest(),
            "theme_vars_sha256": hashlib.sha256((args.output_dir / "theme_vars.json").read_bytes()).hexdigest(),
            "layout_vars_sha256": hashlib.sha256((args.output_dir / "layout_vars.json").read_bytes()).hexdigest(),
            "typst_source_sha256": hashlib.sha256(typst_source_path.read_bytes()).hexdigest(),
        },
    })
    delivery_manifest = args.output_dir / "delivery-manifest.json"
    # Never reuse geometry from an earlier render when this invocation fails
    # before geometry QA is reached.
    (args.output_dir / "geometry-qa.json").unlink(missing_ok=True)
    try:
        geometry = run_pdf_delivery_gate(
            skill_dir=Path(__file__).parent.parent, pdf_path=pdf_path,
            manifest_path=project_manifest_path, typst_path=typst_source_path,
            theme_path=args.output_dir / "theme_vars.json", profile_path=args.profile,
            market=market, geometry_path=args.output_dir / "geometry-qa.json",
            allow_density=args.internal_reflow,
        )
    except ResumeQAError as exc:
        delivery_manifest.unlink(missing_ok=True)
        if args.internal_reflow:
            pdf_path.unlink(missing_ok=True)
        else:
            quarantine_artifacts(
                args.output_dir,
                (pdf_path, typst_source_path, project_manifest_path, args.output_dir / "geometry-qa.json",
                 args.output_dir / "layout_vars.json", args.output_dir / "theme_vars.json"),
                code=exc.code, detail=exc.detail, phase="typst_delivery",
            )
        raise
    if not args.internal_reflow:
        write_delivery_manifest(delivery_manifest, pdf_path=pdf_path, manifest_path=project_manifest_path,
                                typst_path=typst_source_path, theme_path=args.output_dir / "theme_vars.json",
                                geometry=geometry)
    print(json.dumps({"pdf": str(pdf_path), "project_manifest": str(project_manifest_path), "theme_variant": theme["variant_id"], "delivery_manifest": str(delivery_manifest) if delivery_manifest.exists() else None}, ensure_ascii=False))
    return 0


def _cli_output_dir(argv: list[str]) -> Path | None:
    """Best-effort extraction used only by the direct-entry error boundary."""
    try:
        index = argv.index("--output-dir")
        return Path(argv[index + 1]).expanduser().resolve()
    except (ValueError, IndexError):
        return None


def main() -> int:
    """Run the renderer with a quarantine boundary for direct invocations.

    The build orchestrator owns its staging transaction and therefore passes
    ``--internal-reflow``; in that mode errors bubble to the orchestrator so it
    can quarantine the complete render attempt.  A standalone renderer call
    has no parent transaction, so this wrapper creates one and emits the same
    SkillOpt failure event instead of silently leaving a bad partial output.
    """
    try:
        return _main_impl()
    except ResumeQAError:
        # The normal delivery-gate branch has already quarantined this failure.
        raise
    except Exception as exc:
        if "--internal-reflow" not in sys.argv:
            output_dir = _cli_output_dir(sys.argv[1:])
            if output_dir is not None:
                known = (
                    output_dir / "resume.pdf", output_dir / "resume.typ",
                    output_dir / "project-manifest.json", output_dir / "geometry-qa.json",
                    output_dir / "layout_vars.json", output_dir / "theme_vars.json",
                )
                quarantine_artifacts(
                    output_dir, known, code="TYPST_RENDER_ERROR",
                    detail=str(exc), phase="typst_delivery",
                )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
