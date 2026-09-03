#!/usr/bin/env python3
"""Validate the checked-in synthetic fixtures against their public schemas."""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import yaml


ROOT = Path(__file__).resolve().parents[1]


def validate(instance: object, schema_path: Path, label: str) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(f"{label} {list(error.path)}: {error.message}" for error in errors[:5])
        raise SystemExit(f"ERROR: schema validation failed: {details}")


def main() -> int:
    examples = ROOT / "examples"
    validate(yaml.safe_load((examples / "sample.profile.yaml").read_text(encoding="utf-8")), ROOT / "schemas/profile.schema.json", "profile")
    validate(yaml.safe_load((examples / "sample-template.yaml").read_text(encoding="utf-8")), ROOT / "schemas/template.schema.json", "template")
    validate(yaml.safe_load((examples / "sample-inbox.yaml").read_text(encoding="utf-8")), ROOT / "schemas/ingestion-inbox.schema.json", "inbox")
    print("OK: checked-in examples satisfy profile, template, and inbox schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
