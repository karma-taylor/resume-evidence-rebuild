#!/usr/bin/env python3
"""Report whether the community runtime can build and inspect a resume locally."""
from __future__ import annotations

import importlib.util
import argparse
import json
import os
import shutil
import subprocess
import sys


PACKAGES = ("yaml", "pydantic", "docx", "pypdf", "pdfplumber", "PIL", "jsonpatch", "jsonschema")
REQUIRED_CJK_FONT = "Microsoft YaHei"
DEFAULT_TYPST_VERSION = "0.15.1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--font",
        default=os.environ.get("RESUME_CJK_FONT", REQUIRED_CJK_FONT),
        help=f"CJK font family to require (default: $RESUME_CJK_FONT or {REQUIRED_CJK_FONT})",
    )
    parser.add_argument(
        "--typst-version",
        default=os.environ.get("RESUME_TYPST_VERSION", DEFAULT_TYPST_VERSION),
        help=f"Exact Typst version prefix to require (default: $RESUME_TYPST_VERSION or {DEFAULT_TYPST_VERSION})",
    )
    parser.add_argument("--require-docx", action="store_true", help="Also require LibreOffice/soffice for DOCX QA")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit a machine-readable diagnostic object")
    args = parser.parse_args()
    missing = [name for name in PACKAGES if importlib.util.find_spec(name) is None]
    typst = shutil.which("typst")
    available: list[str] = []
    version = ""
    if typst:
        fonts = subprocess.run([typst, "fonts"], text=True, capture_output=True, check=False)
        if args.font.lower() in fonts.stdout.lower():
            available.append(args.font)
        version = subprocess.run([typst, "--version"], text=True, capture_output=True, check=False).stdout.strip()
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    pdftoppm = shutil.which("pdftoppm")
    typst_version_ok = bool(version.startswith(f"typst {args.typst_version}"))
    payload = {
        "status": "ok" if typst and typst_version_ok and not missing and available and pdftoppm and (not args.require_docx or soffice) else "error",
        "python": sys.version.split()[0],
        "python_modules_missing": missing,
        "typst": {"path": typst, "version": version, "expected": args.typst_version, "version_ok": typst_version_ok},
        "font": {"requested": args.font, "available": bool(available)},
        "pdf": {"pdftoppm": pdftoppm},
        "docx": {"required": args.require_docx, "executable": soffice},
    }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if not typst:
            print("ERROR: Typst is not on PATH. Install it before rendering PDF.", file=sys.stderr)
        elif not typst_version_ok:
            print(f"ERROR: Typst version mismatch; expected {args.typst_version}, got {version}.", file=sys.stderr)
        if missing:
            print(f"ERROR: missing Python modules: {', '.join(missing)}", file=sys.stderr)
        if not available:
            print(f"ERROR: required CJK font family missing: {args.font}; install regular and bold faces.", file=sys.stderr)
        if not pdftoppm:
            print("ERROR: pdftoppm is not on PATH; install Poppler for PDF raster QA.", file=sys.stderr)
        if args.require_docx and not soffice:
            print("ERROR: LibreOffice/soffice is not on PATH; install it for DOCX QA.", file=sys.stderr)
        if payload["status"] == "ok":
            print(f"OK: {version}; Python {sys.version.split()[0]}; CJK fonts: {', '.join(available)}")
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
