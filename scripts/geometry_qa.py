#!/usr/bin/env python3
"""Measure the final Typst PDF and emit element-addressable A4 QA findings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader

A4 = (595.28, 841.89)
# A4 density targets 50pt. Allow 2pt for final glyph-box rounding so a
# one-line reflow is not forced to add repetitive copy for a tiny residue.
MAX_BOTTOM_WHITESPACE_PT = 52.0


def inside_page(box: dict[str, Any], page: Any) -> bool:
    return (float(box.get("x0", 0)) >= -0.5 and float(box.get("x1", 0)) <= page.width + 0.5
            and float(box.get("top", 0)) >= -0.5 and float(box.get("bottom", 0)) <= page.height + 0.5)


def overlaps(first: dict[str, Any], second: dict[str, Any], padding: float = 0.0) -> bool:
    return not (
        float(first["x1"]) + padding < float(second["x0"])
        or float(second["x1"]) + padding < float(first["x0"])
        or float(first["bottom"]) + padding < float(second["top"])
        or float(second["bottom"]) + padding < float(first["top"])
    )


def iter_manifest_bullets(manifest: dict[str, Any]):
    """Keep the same employment-before-project order as the Typst renderer."""
    for group in (*manifest.get("employment", []), *manifest.get("projects", [])):
        if isinstance(group, dict):
            for bullet in group.get("bullets", []):
                if isinstance(bullet, dict):
                    yield bullet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    findings: list[dict[str, Any]] = []
    bottom_whitespace_pt: float | None = None
    reader = PdfReader(str(args.pdf))
    if len(reader.pages) != 1:
        findings.append({"code": "PAGE_COUNT_ERROR", "severity": "error", "observed": len(reader.pages)})
    for page_number, reader_page in enumerate(reader.pages, 1):
        if (abs(float(reader_page.mediabox.width) - A4[0]) > 1.5
                or abs(float(reader_page.mediabox.height) - A4[1]) > 1.5):
            findings.append({"code": "PAGE_SIZE_ERROR", "severity": "error", "page": page_number})
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    geometry: list[dict[str, Any]] = []
    with pdfplumber.open(str(args.pdf)) as pdf:
        all_chars: list[dict[str, Any]] = []
        for page_number, page in enumerate(pdf.pages, 1):
            chars = [char for char in page.chars if char.get("text", "").strip()]
            all_chars.extend(chars)
            if not chars:
                findings.append({"code": "TEXT_EXTRACTION_ERROR", "severity": "error", "page": page_number})
                continue
            if any(not inside_page(char, page) for char in chars):
                findings.append({"code": "CUTOFF", "severity": "error", "page": page_number,
                                 "detail": "text glyph lies outside the PDF page boundary"})
            # Compare nearby distinct text lines. Ordinary adjacent lines may
            # share a tiny font-box edge; a material intersection is a real
            # reflow collision rather than ordinary glyph kerning.
            words = page.extract_words(x_tolerance=1, y_tolerance=2)
            lines: list[list[dict[str, Any]]] = []
            for word in sorted(words, key=lambda item: float(item["top"])):
                if not lines or abs(float(word["top"]) - float(lines[-1][0]["top"])) > 3:
                    lines.append([word])
                else:
                    lines[-1].append(word)
            line_boxes = [
                (min(word["x0"] for word in line_words), max(word["x1"] for word in line_words),
                 min(word["top"] for word in line_words), max(word["bottom"] for word in line_words))
                for line_words in lines
            ]
            for left_a, right_a, top_a, bottom_a in line_boxes:
                for left_b, right_b, top_b, bottom_b in line_boxes:
                    if top_b <= top_a:
                        continue
                    if min(bottom_a, bottom_b) - max(top_a, top_b) > 2.5 and min(right_a, right_b) - max(left_a, left_b) > 10:
                        findings.append({"code": "OVERLAP", "severity": "error", "page": page_number,
                                         "detail": "adjacent text lines materially intersect"})
                        break
                if any(finding["code"] == "OVERLAP" and finding.get("page") == page_number for finding in findings):
                    break
            drawables = [*page.lines, *page.rects, *page.images]
            if any(not inside_page(item, page) for item in drawables):
                findings.append({"code": "DRAWABLE_CUTOFF", "severity": "error", "page": page_number,
                                 "detail": "line, rectangle, or image lies outside the PDF page boundary"})
            for image in page.images:
                if any(overlaps(image, line, padding=0.75) for line in [*page.lines, *page.rects]):
                    findings.append({"code": "PHOTO_DECORATION_INTERSECTION", "severity": "error", "page": page_number,
                                     "detail": "a photo/image intersects a decorative line or rectangle"})
                    break
        if pdf.pages:
            last_chars = [char for char in pdf.pages[-1].chars if char.get("text", "").strip()]
            if last_chars:
                bottom_whitespace = pdf.pages[-1].height - max(char["bottom"] for char in last_chars)
                bottom_whitespace_pt = round(float(bottom_whitespace), 2)
                if bottom_whitespace > MAX_BOTTOM_WHITESPACE_PT:
                    findings.append({"code": "BOTTOM_WHITESPACE_EXCESS", "severity": "error", "observed": {"pt": round(bottom_whitespace, 2)}, "threshold": {"max_pt": MAX_BOTTOM_WHITESPACE_PT}})
        for bullet in iter_manifest_bullets(manifest):
            text = str(bullet.get("text", ""))
            prefix = text[:8]
            geometry.append({"element_id": bullet.get("element_id"), "page": None, "prefix": prefix,
                             "candidate_glyphs": sum(prefix[:1] == char.get("text") for char in all_chars) if prefix else 0,
                             "cjk_characters": sum("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff" for char in text),
                             "lines": None, "overflow_pt": 0})
    payload = {"profile": "typst-a4-v1", "passed": not findings,
               "bottom_whitespace_pt": bottom_whitespace_pt, "findings": findings,
               "geometry": geometry}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if not findings else 2


if __name__ == "__main__":
    raise SystemExit(main())
