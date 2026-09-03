#!/usr/bin/env python3
"""Run a safe public PDF smoke test against the synthetic fixtures."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
ABSOLUTE_PATH_RE = re.compile(r'''(?<![A-Za-z0-9_])/(?:[^\s"']+)''')


def validate(instance: object, schema_path: Path, label: str) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(f"{label} {list(error.path)}: {error.message}" for error in errors[:5])
        raise RuntimeError(f"schema validation failed: {details}")


def failure_diagnostics(output: Path) -> dict[str, object]:
    """Return non-sensitive gate facts when the public smoke build fails."""
    diagnostics: dict[str, object] = {}
    paths_by_name: dict[str, list[Path]] = {}
    for name in ("delivery-manifest.json", "geometry-qa.json", "reflow-trace.json", "failed-manifest.json"):
        paths_by_name[name] = [path for path in output.rglob(name) if path.is_file()]
    for name, paths in paths_by_name.items():
        if not paths:
            continue
        path = max(paths, key=lambda candidate: candidate.stat().st_mtime)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if name == "reflow-trace.json":
            rounds = payload.get("rounds") if isinstance(payload, dict) else None
            diagnostics["reflow_status"] = payload.get("status") if isinstance(payload, dict) else None
            diagnostics["reflow_rounds"] = [
                {
                    "round": item.get("round"),
                    "layout_state": item.get("layout_state"),
                    "page_count": item.get("page_count"),
                    "finding_codes": [
                        finding.get("code")
                        for finding in item.get("findings", [])
                        if isinstance(finding, dict) and isinstance(finding.get("code"), str)
                    ],
                    "reason": ABSOLUTE_PATH_RE.sub("<path>", str(item.get("reason")))[:300]
                    if item.get("reason") else None,
                }
                for item in rounds or []
                if isinstance(item, dict)
            ]
        elif name == "geometry-qa.json":
            diagnostics["geometry_finding_codes"] = [
                finding.get("code")
                for finding in payload.get("findings", [])
                if isinstance(finding, dict) and isinstance(finding.get("code"), str)
            ] if isinstance(payload, dict) else []
        elif name == "failed-manifest.json":
            error = payload.get("error") if isinstance(payload, dict) else None
            diagnostics["failure_code"] = error.get("code") if isinstance(error, dict) else None
        else:
            diagnostics["delivery_status"] = payload.get("status") if isinstance(payload, dict) else None
    return diagnostics


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
            details = json.dumps(failure_diagnostics(output), ensure_ascii=False, separators=(",", ":"))
            raise SystemExit(f"ERROR: public smoke build failed\n{completed.stdout}\n{completed.stderr}\ndiagnostics={details}")
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
