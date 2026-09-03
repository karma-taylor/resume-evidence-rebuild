#!/usr/bin/env python3
"""Report whether the community runtime can build and inspect a resume locally."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys


PACKAGES = ("yaml", "pydantic", "docx", "pypdf", "pdfplumber", "PIL", "jsonpatch")
PREFERRED_CJK_FONTS = ("Heiti SC", "Hiragino Sans GB Interface W6", "Noto Sans CJK SC", "Arial Unicode MS")


def main() -> int:
    missing = [name for name in PACKAGES if importlib.util.find_spec(name) is None]
    typst = shutil.which("typst")
    if not typst:
        print("ERROR: Typst is not on PATH. Install it before rendering PDF.", file=sys.stderr)
        return 2
    fonts = subprocess.run([typst, "fonts"], text=True, capture_output=True, check=False)
    available = [font for font in PREFERRED_CJK_FONTS if font.lower() in fonts.stdout.lower()]
    if missing:
        print(f"ERROR: missing Python modules: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not available:
        print("ERROR: no preferred CJK font family found; install a regular and bold CJK font.", file=sys.stderr)
        return 2
    version = subprocess.run([typst, "--version"], text=True, capture_output=True, check=False).stdout.strip()
    print(f"OK: {version}; Python {sys.version.split()[0]}; CJK fonts: {', '.join(available)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
