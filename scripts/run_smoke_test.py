#!/usr/bin/env python3
"""Run a safe public PDF smoke test against the synthetic fixtures."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]


def validate(instance: object, schema_path: Path, label: str) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(f"{label} {list(error.path)}: {error.message}" for error in errors[:5])
        raise RuntimeError(f"schema validation failed: {details}")


def main() -> int:
    with TemporaryDirectory(prefix="resume-evidence-public-smoke-") as temp:
        output = Path(temp) / "output"
        command = [
            sys.executable, str(ROOT / "scripts" / "build_resume.py"),
            "--profile", str(ROOT / "examples" / "sample.profile.yaml"),
            "--template", str(ROOT / "examples" / "sample-template.yaml"),
            "--inbox", str(ROOT / "examples" / "sample-inbox.yaml"),
            "--output-dir", str(output), "--render",
            "--theme-variant", "executive_editorial_a",
        ]
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False, timeout=180)
        if completed.returncode:
            raise SystemExit(f"ERROR: public smoke build failed\n{completed.stdout}\n{completed.stderr}")
        required = ("resume.pdf", "delivery-manifest.json", "project-manifest.json", "geometry-qa.json", "reflow-trace.json", "resume-plan.json", "typeset-plan.json")
        missing = [name for name in required if not (output / name).is_file()]
        if missing:
            raise SystemExit(f"ERROR: public smoke output is missing: {', '.join(missing)}")
        delivery = json.loads((output / "delivery-manifest.json").read_text(encoding="utf-8"))
        if delivery.get("status") != "eligible_for_approval":
            raise SystemExit(f"ERROR: public smoke delivery is not eligible: {delivery.get('status')}")
        validate(json.loads((output / "resume-plan.json").read_text(encoding="utf-8")), ROOT / "schemas" / "resume-plan.schema.json", "resume-plan")
        validate(json.loads((output / "typeset-plan.json").read_text(encoding="utf-8")), ROOT / "schemas" / "agent-b.schema.json", "typeset-plan")
    print("OK: public smoke test passed (PDF, evidence gate, Agent A/B schemas, and delivery manifest)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
