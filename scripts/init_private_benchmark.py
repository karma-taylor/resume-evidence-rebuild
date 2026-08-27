#!/usr/bin/env python3
"""Create ignored, non-runnable 50-fixture redaction scaffolds for human completion."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for number in range(1, 51):
        path = args.output_dir / f"fixture-{number:02d}.json"
        if not path.exists():
            payload = {"fixture_id": f"fixture-{number:02d}", "origin": "manual_redaction_required", "authorized": False, "coverage": [], "sources": [], "sentinels": []}
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Created or retained 50 private scaffold fixtures in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
