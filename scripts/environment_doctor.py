#!/usr/bin/env python3
"""Report whether the community runtime can build and inspect a resume locally."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys


PACKAGES = ("yaml", "pydantic", "docx", "pypdf", "pdfplumber", "PIL", "jsonpatch", "jsonschema")
REQUIRED_CJK_FONT = "Microsoft YaHei"


def main() -> int:
    missing = [name for name in PACKAGES if importlib.util.find_spec(name) is None]
    typst = shutil.which("typst")
    if not typst:
        print("ERROR: Typst is not on PATH. Install it before rendering PDF.", file=sys.stderr)
        return 2
    fonts = subprocess.run([typst, "fonts"], text=True, capture_output=True, check=False)
    available = [REQUIRED_CJK_FONT] if REQUIRED_CJK_FONT.lower() in fonts.stdout.lower() else []
    if missing:
        print(f"ERROR: missing Python modules: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not available:
        print(f"ERROR: required CJK font family missing: {REQUIRED_CJK_FONT}; install regular and bold faces.", file=sys.stderr)
        return 2
    version = subprocess.run([typst, "--version"], text=True, capture_output=True, check=False).stdout.strip()
    print(f"OK: {version}; Python {sys.version.split()[0]}; CJK fonts: {', '.join(available)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
