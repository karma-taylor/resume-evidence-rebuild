#!/usr/bin/env python3
"""Reject incomplete, fabricated, or non-redacted benchmark fixtures."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", type=Path, required=True)
    args = parser.parse_args()
    fixtures = sorted(args.fixture_root.glob("fixture-*.json"))
    if len(fixtures) != 50:
        raise SystemExit(f"BENCHMARK_INCOMPLETE: expected 50 fixtures, found {len(fixtures)}")
    for path in fixtures:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("origin") != "human_redacted" or payload.get("authorized") is not True:
            raise SystemExit(f"BENCHMARK_INCOMPLETE: {path.name} is not an authorized human-redacted fixture")
        if not payload.get("coverage") or not payload.get("sources") or not payload.get("sentinels"):
            raise SystemExit(f"BENCHMARK_INCOMPLETE: {path.name} lacks coverage, sources, or sentinels")
    print("OK: 50 authorized human-redacted benchmark fixtures are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
