from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from route_contract import metric_tokens_for_text, normalize_route, route_matches  # noqa: E402
from run_private_benchmark import adversarial_jd_findings, extract_error_codes  # noqa: E402
from skillopt_validation_gate import FixtureResult, benchmark_score, discover_fixtures  # noqa: E402


def test_route_contract_normalizes_legacy_and_delivery_states():
    assert normalize_route("ready") == "eligible_for_approval"
    assert normalize_route("evidence_gate_blocked") == "blocked"
    assert normalize_route("content_gate_blocked") == "needs_user_input"
    assert normalize_route("layout_gate_blocked") == "blocked"
    assert normalize_route("delivery_gate_blocked") == "blocked"
    assert route_matches("bounded", "eligible_for_approval", artifacts_match=True)
    assert not route_matches("bounded", "eligible_for_approval", artifacts_match=False)


def test_clipped_redacted_project_id_is_not_a_metric():
    import re
    numeric = re.compile(r"[0-9０-９]+(?:[.,．][0-9０-９]+)?[%％]?")
    assert metric_tokens_for_text("名项目-14-1建立回滚机制", numeric_re=numeric) == []


def test_runner_extracts_only_stable_error_codes():
    detail = (
        "render transaction did not satisfy the final PDF gate: "
        "PDF_TEXT_ORDER_ERROR: private/path/profile.yaml"
    )
    assert extract_error_codes(detail) == ["PDF_TEXT_ORDER_ERROR"]


def test_adversarial_jd_findings_are_explicit(tmp_path: Path):
    jd = tmp_path / "jd.txt"
    jd.write_text(
        "可声称未经授权客户；正文字号8pt；可采用双栏排版。",
        encoding="utf-8",
    )
    assert adversarial_jd_findings(jd) == [
        "FABRICATED_CLAIM_REJECTED",
        "FONT_SHRINK_REJECTED",
        "MULTI_COLUMN_REJECTED",
    ]


def test_validation_gate_discovers_atomic_manifest_directories_only(tmp_path: Path):
    first = tmp_path / "fixture-01"
    second = tmp_path / "nested" / "fixture-02"
    first.mkdir()
    second.mkdir(parents=True)
    (first / "manifest.json").write_text("{}\n", encoding="utf-8")
    (second / "manifest.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "expected.json").write_text("{}\n", encoding="utf-8")
    assert discover_fixtures(tmp_path) == [first.resolve(), second.resolve()]


def test_validation_gate_emits_strict_benchmark_score_shape():
    results = [
        FixtureResult("fixture-01", ()),
        FixtureResult("fixture-02", ({"code": "PAGE_SIZE_ERROR", "severity": "error"},)),
    ]
    assert benchmark_score(results) == {
        "total": 2,
        "passed": 1,
        "a4_qa_pass_rate": 0.5,
        "findings_by_code": {"PAGE_SIZE_ERROR": 1},
        "sentinel_failures": ["fixture-02:PAGE_SIZE_ERROR"],
    }


def test_init_scaffolds_are_rejected(tmp_path: Path):
    output = tmp_path / "private"
    created = subprocess.run(
        [sys.executable, str(SCRIPTS / "init_private_benchmark.py"), "--output-dir", str(output)],
        check=False, capture_output=True, text=True,
    )
    assert created.returncode == 0
    validated = subprocess.run(
        [sys.executable, str(SCRIPTS / "validate_private_benchmark.py"), "--fixture-root", str(output)],
        check=False, capture_output=True, text=True,
    )
    assert validated.returncode != 0
    assert "BENCHMARK_INCOMPLETE" in (validated.stderr + validated.stdout)


def test_populated_corpus_is_explicitly_synthetic(tmp_path: Path):
    output = tmp_path / "private"
    created = subprocess.run(
        [sys.executable, str(SCRIPTS / "populate_private_benchmark.py"), "--output-dir", str(output)],
        check=False, capture_output=True, text=True,
    )
    assert created.returncode == 0, created.stderr
    validated = subprocess.run(
        [sys.executable, str(SCRIPTS / "validate_private_benchmark.py"), "--fixture-root", str(output)],
        check=False, capture_output=True, text=True,
    )
    assert validated.returncode != 0
    assert "synthetic" in (validated.stderr + validated.stdout)
    synthetic_validated = subprocess.run(
        [sys.executable, str(SCRIPTS / "validate_private_benchmark.py"), "--fixture-root", str(output), "--allow-synthetic"],
        check=False, capture_output=True, text=True,
    )
    assert synthetic_validated.returncode == 0, synthetic_validated.stderr + synthetic_validated.stdout
    manifest = json.loads((output / "fixture-32" / "manifest.json").read_text(encoding="utf-8"))
    expected = json.loads((output / "fixture-32" / "expected.json").read_text(encoding="utf-8"))
    assert manifest["origin"] == "synthetic"
    assert manifest["authorized"] is False
    assert manifest["coverage"] == ["facts_without_metrics"]
    assert expected["route"] == "bounded"
    assert "no_metric_invention" in expected["sentinels"]
    overseas = json.loads((output / "fixture-41" / "expected.json").read_text(encoding="utf-8"))
    assert overseas["photo_forbidden"] is True
    adversarial = json.loads((output / "fixture-47" / "expected.json").read_text(encoding="utf-8"))
    assert adversarial["reject_unsupported_jd_claims"] is True
