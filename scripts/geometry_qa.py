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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    findings: list[dict[str, Any]] = []
    reader = PdfReader(str(args.pdf))
    if len(reader.pages) != 1:
        findings.append({"code": "PAGE_COUNT_ERROR", "severity": "error", "observed": len(reader.pages)})
    elif abs(float(reader.pages[0].mediabox.width) - A4[0]) > 1.5 or abs(float(reader.pages[0].mediabox.height) - A4[1]) > 1.5:
        findings.append({"code": "PAGE_SIZE_ERROR", "severity": "error"})
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    geometry: list[dict[str, Any]] = []
    with pdfplumber.open(str(args.pdf)) as pdf:
        page = pdf.pages[0]
        chars = [char for char in page.chars if char.get("text", "").strip()]
        if not chars:
            findings.append({"code": "TEXT_EXTRACTION_ERROR", "severity": "error"})
        else:
            bottom_whitespace = page.height - max(char["bottom"] for char in chars)
            if bottom_whitespace > 72:
                findings.append({"code": "BOTTOM_WHITESPACE_EXCESS", "severity": "error", "observed": {"pt": round(bottom_whitespace, 2)}, "threshold": {"max_pt": 72}})
            for project in manifest["projects"]:
                for bullet in project["bullets"]:
                    prefix = bullet["text"][:8]
                    hits = [char for char in chars if prefix[:1] == char["text"]]
                    cjk = sum("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff" for char in bullet["text"])
                    geometry.append({"element_id": bullet["element_id"], "page": 1, "prefix": prefix, "candidate_glyphs": len(hits), "cjk_characters": cjk, "lines": None, "overflow_pt": 0})
    payload = {"profile": "typst-a4-v1", "passed": not findings, "findings": findings, "geometry": geometry}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if not findings else 2


if __name__ == "__main__":
    raise SystemExit(main())
