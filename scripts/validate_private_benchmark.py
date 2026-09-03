#!/usr/bin/env python3
"""Reject incomplete, fabricated, or non-redacted benchmark fixtures."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
MATERIAL_FILES = ("resume.txt", "jd.txt", "evidence.txt")
REQUIRED_RELATIVE = (
    "manifest.json",
    "profile.yaml",
    "template.yaml",
    "expected.json",
    "materials/resume.txt",
    "materials/jd.txt",
    "materials/evidence.txt",
    "materials/inbox.yaml",
)
BASE_SENTINELS = ("no-fabrication", "source-traceability", "single-a4", "privacy")
COVERAGE_SLICES = (
    ("cn_ai_normal_density", frozenset(range(1, 16)), 15, ()),
    ("sparse_whitespace_risk", frozenset(range(16, 24)), 8, ()),
    ("dense_overflow_risk", frozenset(range(24, 32)), 8, ("no_font_shrink", "no_multi_column")),
    ("facts_without_metrics", frozenset(range(32, 37)), 5, ("no_metric_invention",)),
    ("missing_project_evidence", frozenset(range(37, 41)), 4, ("must_request_user_input",)),
    ("na_foreign_no_photo", frozenset(range(41, 44)), 3, ("photo_forbidden",)),
    ("timeline_contact_integrity", frozenset(range(44, 47)), 3, ()),
    ("adversarial_jd", frozenset(range(47, 51)), 4, ("reject_unsupported_claim",)),
)
CLAIM_KINDS = {"context", "architecture", "control", "metric", "delivery"}
PII_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|password|secret|token|bearer)\b"),
    re.compile(r"(?i)github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
    re.compile(r"(?i)gitlab\.com/[A-Za-z0-9_.-]+"),
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"(?i)[A-Z0-9._%+-]+@(?!example\.(?:com|invalid)\b)[A-Z0-9.-]+\.[A-Z]{2,}"),
)
NUMERIC_RE = re.compile(r"[0-9０-９]")


def fail(message: str) -> None:
    raise SystemExit(f"BENCHMARK_INCOMPLETE: {message}")


def load_schema(name: str) -> Draft202012Validator:
    schema = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_yaml(path: Path) -> object:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if payload is None:
        fail(f"{path} is empty")
    return payload


def scan_pii(label: str, text: str) -> None:
    for pattern in PII_PATTERNS:
        if pattern.search(text):
            fail(f"{label} appears to contain real contact, credential, or repository data")


def slice_for(number: int) -> tuple[str, tuple[str, ...]]:
    for name, numbers, _count, extra in COVERAGE_SLICES:
        if number in numbers:
            return name, extra
    fail(f"fixture-{number:02d} is outside the frozen 1-50 coverage map")
    raise AssertionError


def validate_profile(path: Path, coverage: str) -> dict:
    profile = read_yaml(path)
    if not isinstance(profile, dict):
        fail(f"{path} must be a mapping")
    identity = profile.get("identity")
    if not isinstance(identity, dict):
        fail(f"{path} is missing identity")
    for field in ("name", "phone", "email", "portfolio_url", "market"):
        if not str(identity.get(field, "")).strip():
            fail(f"{path} identity.{field} is required")
    education = profile.get("education")
    if not isinstance(education, list) or not education:
        fail(f"{path} must include education")
    for item in education:
        if not isinstance(item, dict) or any(not item.get(key) for key in ("school", "degree", "major", "start", "end")):
            fail(f"{path} education entries need school, degree, major, start, and end")
    employment = profile.get("employment")
    if not isinstance(employment, list) or not employment:
        fail(f"{path} must include employment")
    for job in employment:
        if not isinstance(job, dict):
            fail(f"{path} employment entries must be mappings")
        highlights = job.get("highlights")
        if not isinstance(highlights, list) or not 4 <= len(highlights) <= 5:
            fail(f"{path} each employment block needs 4-5 sourced facts")
        for highlight in highlights:
            if not isinstance(highlight, dict):
                fail(f"{path} employment facts must be mappings")
            for key in ("text", "source_ingestion_id", "approved_at", "source_hash"):
                if not highlight.get(key):
                    fail(f"{path} employment fact missing {key}")
            if not re.fullmatch(r"[a-fA-F0-9]{64}", str(highlight["source_hash"])):
                fail(f"{path} employment source_hash must be SHA-256")
    projects = profile.get("projects")
    if not isinstance(projects, list) or len(projects) < 3:
        fail(f"{path} must include at least 3 projects")
    for project in projects:
        if not isinstance(project, dict):
            fail(f"{path} projects must be mappings")
        claims = project.get("claims")
        if not isinstance(claims, list) or len(claims) < 3:
            fail(f"{path} each project needs at least 3 claims")
        kinds = {claim.get("kind") for claim in claims if isinstance(claim, dict)}
        if not kinds.issubset(CLAIM_KINDS):
            fail(f"{path} claim kinds must be context/architecture/control/metric/delivery")
        for claim in claims:
            if not isinstance(claim, dict):
                fail(f"{path} claims must be mappings")
            for key in ("source", "scope", "confidence", "allowed_for_resume"):
                if key not in claim:
                    fail(f"{path} claim missing {key}")
            if coverage == "facts_without_metrics":
                if claim.get("kind") == "metric" or NUMERIC_RE.search(str(claim.get("text", ""))):
                    fail(f"{path} no-metric fixture contains a metric or numeric claim")
            if "redacted_metric" in str(claim.get("text", "")) and coverage == "facts_without_metrics":
                fail(f"{path} no-metric fixture cannot carry redacted_metric text")
    if coverage == "na_foreign_no_photo" and identity.get("market") not in {"NA", "FOREIGN"}:
        fail(f"{path} overseas fixture must use NA or FOREIGN market")
    if "photo_path" in identity and coverage == "na_foreign_no_photo":
        fail(f"{path} overseas fixture must not include a photo path")
    return profile


def validate_template(path: Path, coverage: str, profile: dict) -> None:
    template = read_yaml(path)
    if not isinstance(template, dict):
        fail(f"{path} must be a mapping")
    for key in ("target_role", "market", "project_ids", "sections"):
        if key not in template:
            fail(f"{path} missing {key}")
    layout = template.get("layout")
    if not isinstance(layout, dict):
        fail(f"{path} missing layout")
    if layout.get("page") != "A4" or layout.get("columns") != 1:
        fail(f"{path} must lock single-column A4")
    if not isinstance(layout.get("min_body_pt"), (int, float)) or layout["min_body_pt"] < 10:
        fail(f"{path} body type size must be at least 10pt")
    if layout.get("body_line_height_multiplier") != 1.4:
        fail(f"{path} must freeze 1.4 body line height")
    project_ids = template.get("project_ids")
    profile_ids = {project.get("id") for project in profile.get("projects", []) if isinstance(project, dict)}
    if not isinstance(project_ids, list) or not 3 <= len(project_ids) <= 4:
        fail(f"{path} needs 3-4 default projects")
    if coverage == "na_foreign_no_photo" and template.get("market") not in {"NA", "FOREIGN"}:
        fail(f"{path} overseas fixture template must be NA or FOREIGN")
    if coverage != "missing_project_evidence" and not set(project_ids).issubset(profile_ids):
        fail(f"{path} default projects must exist in the redacted profile")


def validate_expected(path: Path, number: int, coverage: str, extra: tuple[str, ...], sentinels: list[str]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(load_schema("benchmark-expected.schema.json").iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        fail(f"{path.name}: {errors[0].message}")
    if payload.get("fixture_id") != f"fixture-{number:02d}":
        fail(f"{path} fixture_id mismatch")
    if set(payload.get("sentinels") or []) != set(sentinels):
        fail(f"{path} sentinels must match the manifest")
    route = payload.get("route")
    if coverage == "facts_without_metrics" and route != "bounded":
        fail(f"{path} no-metric fixture must expect bounded routing")
    if coverage == "missing_project_evidence" and route != "needs_user_input":
        fail(f"{path} missing-evidence fixture must expect needs_user_input")
    if coverage == "na_foreign_no_photo" and payload.get("photo_forbidden") is not True:
        fail(f"{path} overseas fixture must forbid photos")
    if coverage == "adversarial_jd" and payload.get("reject_unsupported_jd_claims") is not True:
        fail(f"{path} adversarial fixture must reject unsupported JD claims")
    if extra and any(item not in payload.get("sentinels", []) for item in extra):
        fail(f"{path} missing coverage sentinels {extra}")
    if coverage == "missing_project_evidence":
        codes = payload.get("error_codes") or []
        if "INSUFFICIENT_PROJECT_EVIDENCE" not in codes and "NEEDS_USER_INPUT" not in codes:
            fail(f"{path} missing-evidence fixture needs INSUFFICIENT_PROJECT_EVIDENCE or NEEDS_USER_INPUT")
        if payload.get("generate_pdf") or payload.get("generate_docx"):
            fail(f"{path} missing-evidence fixture must not expect rendered artifacts")


def validate_fixture(directory: Path, number: int, *, allow_synthetic: bool = False) -> str:
    fixture_id = f"fixture-{number:02d}"
    if directory.name != fixture_id:
        fail(f"{directory} must be named {fixture_id}")
    for relative in REQUIRED_RELATIVE:
        if not (directory / relative).is_file():
            fail(f"{fixture_id} is missing {relative}")
    for path in directory.rglob("*"):
        if path.is_file():
            scan_pii(str(path.relative_to(directory)), path.read_text(encoding="utf-8", errors="ignore"))
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
                fail(f"{fixture_id} contains a forbidden binary {path.name}")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    is_synthetic = manifest.get("origin") == "synthetic" and manifest.get("authorized") is False
    if is_synthetic and not allow_synthetic:
        fail(f"{fixture_id} is synthetic; use --allow-synthetic only for parser/layout tests")
    manifest_schema = "benchmark-synthetic-manifest.schema.json" if is_synthetic else "benchmark-manifest.schema.json"
    errors = sorted(load_schema(manifest_schema).iter_errors(manifest), key=lambda item: list(item.path))
    if errors:
        fail(f"{fixture_id} manifest: {errors[0].message}")
    if manifest.get("fixture_id") != fixture_id:
        fail(f"{fixture_id} manifest fixture_id mismatch")
    coverage, extra = slice_for(number)
    if manifest.get("coverage") != [coverage]:
        fail(f"{fixture_id} coverage must be [{coverage}]")
    sentinels = list(BASE_SENTINELS) + list(extra)
    if set(manifest.get("sentinels") or []) != set(sentinels):
        fail(f"{fixture_id} sentinels must be {sentinels}")
    if any("真实" in source or "http" in source.lower() or "/" in source for source in manifest.get("sources", [])):
        fail(f"{fixture_id} sources must stay generalized")
    files = manifest.get("files") or {}
    for relative in REQUIRED_RELATIVE:
        if relative == "manifest.json":
            continue
        digest = files.get(relative)
        actual = sha256_file(directory / relative)
        if digest != actual:
            fail(f"{fixture_id} files[{relative}] is not the current SHA-256")
    for name in MATERIAL_FILES:
        relative = f"materials/{name}"
        if relative not in files:
            fail(f"{fixture_id} materials hash missing for {name}")
    profile = validate_profile(directory / "profile.yaml", coverage)
    validate_template(directory / "template.yaml", coverage, profile)
    validate_expected(directory / "expected.json", number, coverage, extra, sentinels)
    return coverage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--allow-synthetic", action="store_true", help="Allow synthetic fixtures for non-gold parser/layout tests")
    args = parser.parse_args()
    root = args.fixture_root
    if not root.is_dir():
        fail(f"{root} is not a directory")
    fixtures = sorted(path for path in root.glob("fixture-*") if path.is_dir())
    if len(fixtures) != 50:
        fail(f"expected 50 fixture directories, found {len(fixtures)}")
    numbers = []
    coverages: list[str] = []
    for directory in fixtures:
        match = re.fullmatch(r"fixture-(\d{2})", directory.name)
        if not match:
            fail(f"unexpected directory name {directory.name}")
        number = int(match.group(1))
        numbers.append(number)
        coverages.append(validate_fixture(directory, number, allow_synthetic=args.allow_synthetic))
    if numbers != list(range(1, 51)):
        fail("fixture directories must be exactly fixture-01 through fixture-50")
    counts = Counter(coverages)
    expected_counts = {name: count for name, _numbers, count, _extra in COVERAGE_SLICES}
    if dict(counts) != expected_counts:
        fail(f"coverage counts {dict(counts)} do not match {expected_counts}")
    label = "synthetic test" if args.allow_synthetic else "authorized human-redacted"
    print(f"OK: 50 {label} benchmark fixtures are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
