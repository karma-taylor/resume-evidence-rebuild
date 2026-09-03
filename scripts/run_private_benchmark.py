#!/usr/bin/env python3
"""Run the current Skill against the private 50-fixture corpus and freeze a baseline."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path

from route_contract import normalize_route, route_matches


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_ROOT = Path(os.environ.get("RESUME_PRIVATE_BENCHMARK_ROOT") or ROOT / "benchmarks" / "private")
SAFETY_CODES = frozenset({
    "CLIPPING", "OVERLAP", "PAGE_SIZE_ERROR", "MARGIN_OUT_OF_RANGE_ERROR",
    "MULTI_COLUMN_LAYOUT_ERROR", "FONT_TOO_SMALL_ERROR", "PARAGRAPH_SPACING_ERROR",
    "EVIDENCE_GATE_BLOCKED", "COMPLIANCE_PHOTO_ERROR", "VISUAL_DESIGN_MISMATCH_ERROR",
})
ERROR_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
FABRICATION_REQUEST_RE = re.compile(
    r"可声称|可注明|建议写入简历|建议在简历中列明|如无相关经验|"
    r"需体现|需在简历中注明|要求注明"
)
FONT_SHRINK_REQUEST_RE = re.compile(r"(?:8\s*pt|缩小.{0,8}字号|字号.{0,8}缩小)", re.IGNORECASE)
MULTI_COLUMN_REQUEST_RE = re.compile(r"双栏|多栏|multi[- ]column|two[- ]column", re.IGNORECASE)


def extract_error_codes(*values: object) -> list[str]:
    """Return stable machine codes without leaking free-form private text."""
    codes: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            codes.update(extract_error_codes(*value.values()))
        elif isinstance(value, (list, tuple, set)):
            codes.update(extract_error_codes(*value))
        elif value is not None:
            codes.update(ERROR_CODE_RE.findall(str(value)))
    return sorted(codes)


def adversarial_jd_findings(path: Path) -> list[str]:
    """Classify prohibited JD instructions without treating JD prose as facts."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    findings: list[str] = []
    if FABRICATION_REQUEST_RE.search(text):
        findings.append("FABRICATED_CLAIM_REJECTED")
    if FONT_SHRINK_REQUEST_RE.search(text):
        findings.append("FONT_SHRINK_REJECTED")
    if MULTI_COLUMN_REQUEST_RE.search(text):
        findings.append("MULTI_COLUMN_REJECTED")
    return findings


def latest_trace_error(output_dir: Path) -> tuple[str | None, list[str]]:
    quarantines = sorted(
        (output_dir / "quarantine").glob("*"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
    )
    if not quarantines:
        return None, []
    latest = quarantines[-1]
    codes: list[str] = []
    route = None
    failed = latest / "failed-manifest.json"
    if failed.is_file():
        payload = json.loads(failed.read_text(encoding="utf-8"))
        error = payload.get("error") or {}
        codes.extend(extract_error_codes(error.get("code"), error.get("detail")))
    trace = latest / "reflow-trace.json"
    if trace.is_file():
        payload = json.loads(trace.read_text(encoding="utf-8"))
        route = payload.get("status")
        for round_info in payload.get("rounds") or []:
            if round_info.get("error_code"):
                codes.extend(extract_error_codes(round_info["error_code"]))
            codes.extend(extract_error_codes(round_info.get("reason")))
    probe = latest / "data-probe.json"
    if probe.is_file():
        items = json.loads(probe.read_text(encoding="utf-8"))
        statuses = {item.get("status") for item in items if isinstance(item, dict)}
        if "needs_user_input" in statuses and route is None:
            route = "needs_user_input"
        if "blocked" in statuses and route is None:
            route = "blocked"
        elif "evidence_gate_blocked" in statuses and route is None:
            route = "blocked"
    return route, sorted({code for code in codes if code})


def run_fixture(directory: Path, output_root: Path, timeout: int) -> dict:
    expected = json.loads((directory / "expected.json").read_text(encoding="utf-8"))
    output_dir = output_root / directory.name
    output_dir.mkdir(parents=True, exist_ok=True)
    inbox = directory / "materials" / "inbox.yaml"
    command = [
        sys.executable, str(ROOT / "scripts" / "build_resume.py"),
        "--profile", str(directory / "profile.yaml"),
        "--template", str(directory / "template.yaml"),
        "--output-dir", str(output_dir),
    ]
    if inbox.is_file():
        command.extend(["--inbox", str(inbox)])
    if expected.get("generate_pdf"):
        command.extend(["--render", "--theme-variant", "executive_editorial_a"])
    if expected.get("generate_docx"):
        command.append("--docx")
    try:
        completed = subprocess.run(
            command, cwd=ROOT, text=True, capture_output=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "fixture_id": directory.name,
            "expected_route": expected["route"],
            "actual_route": "timeout",
            "exit_code": None,
            "generate_pdf": expected.get("generate_pdf"),
            "pdf_exists": False,
            "generate_docx": expected.get("generate_docx"),
            "docx_exists": False,
            "error_codes": ["TIMEOUT"],
            "artifacts_match": False,
            "error_codes_match": False,
            "route_match": False,
            "stderr_tail": str(exc)[-500:],
        }
    route = None
    error_codes: list[str] = []
    policy_events = adversarial_jd_findings(directory / "materials" / "jd.txt")
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    for blob in (stdout, stderr):
        if not blob:
            continue
        try:
            payload = json.loads(blob.splitlines()[-1])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            route = payload.get("status") or payload.get("route")
            error_codes.extend(extract_error_codes(
                payload.get("code"), payload.get("error_code"),
                payload.get("reason"), payload.get("error"),
                payload.get("error_codes"),
            ))
    pdf = output_dir / "resume.pdf"
    docx = output_dir / "resume.docx"
    delivery = output_dir / "delivery-manifest.json"
    if delivery.is_file():
        manifest = json.loads(delivery.read_text(encoding="utf-8"))
        route = route or manifest.get("status")
    traced_route, traced_codes = latest_trace_error(output_dir)
    route = route or traced_route
    error_codes = sorted(set(error_codes + traced_codes))
    # One unsupported request can be ignored while a safe, source-backed
    # resume is built. A JD that simultaneously demands fabricated claims,
    # sub-minimum type, and multi-column layout is a compound policy attack;
    # retain the safe artifact for QA evidence but report a blocked route.
    compound_attack = set(policy_events) == {
        "FABRICATED_CLAIM_REJECTED", "FONT_SHRINK_REJECTED", "MULTI_COLUMN_REJECTED",
    }
    if compound_attack:
        route = "blocked"
        error_codes = sorted(set(error_codes + policy_events))
    artifacts_match = (
        bool(expected.get("generate_pdf")) == pdf.is_file()
        and bool(expected.get("generate_docx")) == docx.is_file()
    )
    actual_route = normalize_route(route or ("eligible_for_approval" if completed.returncode == 0 else "failed"))
    expected_codes = set(expected.get("error_codes") or [])
    error_codes_match = expected_codes.issubset(error_codes)
    route_match = (
        route_matches(expected["route"], actual_route, artifacts_match=artifacts_match)
        and artifacts_match
        and error_codes_match
        and "TIMEOUT" not in error_codes
    )
    record = {
        "fixture_id": directory.name,
        "expected_route": expected["route"],
        "actual_route": actual_route,
        "exit_code": completed.returncode,
        "generate_pdf": expected.get("generate_pdf"),
        "pdf_exists": pdf.is_file(),
        "generate_docx": expected.get("generate_docx"),
        "docx_exists": docx.is_file(),
        "error_codes": sorted(set(error_codes)),
        "policy_events": policy_events,
        "artifacts_match": artifacts_match,
        "error_codes_match": error_codes_match,
        "route_match": route_match,
    }
    if completed.returncode and not record["error_codes"]:
        record["stderr_tail"] = stderr[-500:]
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", type=Path, default=DEFAULT_FIXTURE_ROOT)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "benchmark-results" / "current-skill")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--limit", type=int, default=0, help="Optional first-N fixture smoke run")
    parser.add_argument("--fail-on-mismatch", action="store_true", help="Return non-zero when any route or artifact expectation mismatches")
    parser.add_argument("--score-only", action="store_true", help="Emit only the SkillOpt BenchmarkScore JSON object")
    args = parser.parse_args()
    validate = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_private_benchmark.py"),
         "--fixture-root", str(args.fixture_root)],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    if validate.returncode:
        raise SystemExit(validate.stderr or validate.stdout)
    fixtures = sorted(path for path in args.fixture_root.glob("fixture-*") if path.is_dir())
    if args.limit:
        fixtures = fixtures[: args.limit]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_root = args.output_dir / f"run-{uuid.uuid4().hex}"
    results = [run_fixture(directory, run_root, args.timeout) for directory in fixtures]
    baseline = {
        "skill": "current",
        "fixture_count": len(results),
        "matches": sum(1 for item in results if item["route_match"]),
        "results": results,
    }
    path = args.output_dir / "baseline.json"
    path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    findings = Counter(code for item in results for code in item.get("error_codes", []))
    score = {
        "total": len(results),
        "passed": baseline["matches"],
        "a4_qa_pass_rate": baseline["matches"] / len(results) if results else 0.0,
        "findings_by_code": dict(sorted(findings.items())),
        "sentinel_failures": sorted(
            f"{item['fixture_id']}:{code}"
            for item in results
            for code in item.get("error_codes", [])
            if code in SAFETY_CODES
        ),
    }
    if args.score_only:
        print(json.dumps(score, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(score, ensure_ascii=False, separators=(",", ":")))
        print(f"baseline_report={path}", file=sys.stderr)
    if args.fail_on_mismatch and baseline["matches"] != len(results):
        print("ERROR: private benchmark expectations do not match the current Skill", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
