#!/usr/bin/env python3
"""Generate three visual-review candidates and explicitly approve one."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from design_tokens import DESIGN_VARIANTS, load_theme, theme_payload, theme_review_payload


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review", help="write the three safe executive-editorial candidates")
    review.add_argument("--output", type=Path, required=True)
    approve = commands.add_parser("approve", help="write one approved token file")
    approve.add_argument("--variant", choices=tuple(DESIGN_VARIANTS), required=True)
    approve.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate", help="verify an approved token file")
    validate.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "review":
            write(args.output, theme_review_payload())
            print(json.dumps({"status": "theme_review_pending", "review": str(args.output)}, ensure_ascii=False))
        elif args.command == "approve":
            write(args.output, theme_payload(args.variant))
            print(json.dumps({"status": "approved", "variant_id": args.variant, "theme_vars": str(args.output)}, ensure_ascii=False))
        else:
            approved = load_theme(args.input)
            print(json.dumps({"status": "valid", "variant_id": approved["variant_id"]}, ensure_ascii=False))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
