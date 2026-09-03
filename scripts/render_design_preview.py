#!/usr/bin/env python3
"""Render all approved visual candidates into a single review PNG without auto-approving any."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from design_tokens import DESIGN_VARIANTS, theme_payload, theme_review_payload


def render_variant(args: argparse.Namespace, variant: str, output_dir: Path) -> dict:
    """Create a static normal-layout preview only; never invoke Reflow or QA."""
    output_dir.mkdir(parents=True, exist_ok=True)
    planning = [
        sys.executable, str(Path(__file__).with_name("build_resume.py")),
        "--profile", str(args.profile), "--template", str(args.template),
        "--agent-b-output", str(args.agent_b_output), "--output-dir", str(output_dir),
    ]
    completed = subprocess.run(planning, text=True, capture_output=True)
    theme_path = output_dir / "theme_vars.json"
    theme_path.write_text(json.dumps(theme_payload(variant), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    layout_path = output_dir / "layout_vars.json"
    layout_path.write_text(json.dumps({"reflow_round": 0, "layout_state": "normal", "spacing": {
        "header_to_first_module": "18pt", "module_gap": "14pt", "project_gap": "12pt",
        "title_to_overview": "4pt", "overview_to_bullet": "4pt",
    }, "feedback_trace": {"previous_page_count": None, "previous_bottom_whitespace_pt": None, "action_taken": "theme_preview_only"}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if completed.returncode == 0:
        completed = subprocess.run([
            sys.executable, str(Path(__file__).with_name("typst_renderer.py")),
            "--profile", str(args.profile), "--template", str(args.template),
            "--resume-plan", str(output_dir / "resume-plan.json"), "--typeset-plan", str(output_dir / "typeset-plan.json"),
            "--output-dir", str(output_dir), "--layout-vars", str(layout_path), "--theme-vars", str(theme_path),
        ], text=True, capture_output=True)
    pdf, png = output_dir / "resume.pdf", output_dir / "preview.png"
    if pdf.is_file():
        converter = shutil.which("pdftoppm")
        if converter:
            subprocess.run([converter, "-png", "-f", "1", "-singlefile", str(pdf), str(output_dir / "preview")], check=True)
    return {
        "variant_id": variant,
        "status": "theme_review_pending",
        "exit_code": completed.returncode,
        "pdf": str(pdf) if pdf.is_file() else None,
        "png": str(png) if png.is_file() else None,
        "log": (completed.stdout + completed.stderr).strip(),
    }


def compose(results: list[dict], destination: Path) -> None:
    images = [(item, Image.open(item["png"]).convert("RGB")) for item in results if item["png"]]
    if not images:
        return
    width = 420
    label_height, gap = 48, 18
    thumbs = [(item, image.resize((width, round(image.height * width / image.width)))) for item, image in images]
    canvas = Image.new("RGB", (sum(image.width for _, image in thumbs) + gap * (len(thumbs) - 1), max(image.height for _, image in thumbs) + label_height), "#F8FAFC")
    draw = ImageDraw.Draw(canvas)
    x = 0
    for item, image in thumbs:
        canvas.paste(image, (x, label_height))
        text = f"{item['variant_id']} — preview only"
        draw.text((x + 8, 14), text, fill="#1D2630")
        x += image.width + gap
    canvas.save(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--agent-b-output", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    review_path = args.output_dir / "design-review.json"
    review_path.write_text(json.dumps(theme_review_payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    results = [render_variant(args, variant, args.output_dir / variant) for variant in DESIGN_VARIANTS]
    compose(results, args.output_dir / "design-review.png")
    result_path = args.output_dir / "design-preview-results.json"
    result_path.write_text(json.dumps({"review": str(review_path), "candidates": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "theme_review_pending", "review": str(review_path), "preview": str(args.output_dir / "design-review.png"), "results": str(result_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
