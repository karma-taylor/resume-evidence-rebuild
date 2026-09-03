from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from skillopt_auto_loop import (  # noqa: E402
    AutoLoopError,
    build_event,
    diagnose,
    rollback,
    run_optimizer,
)
from validate_resume_artifacts import (  # noqa: E402
    begin_render_transaction,
    quarantine_render_transaction,
)


def failed_manifest(tmp_path: Path, code: str = "PARAGRAPH_SPACING_ERROR") -> Path:
    output = tmp_path / "output"
    run_id, staging = begin_render_transaction(output)
    (staging / "reflow-trace.json").write_text(json.dumps({"status": "failed"}), encoding="utf-8")
    quarantine = quarantine_render_transaction(
        output, run_id, staging, code=code, detail="test failure", phase="docx_delivery",
        inputs={"skill_sha256": "a" * 64, "template_sha256": "b" * 64},
    )
    return quarantine / "failed-manifest.json"


def test_diagnose_is_redacted_and_idempotent(tmp_path: Path):
    manifest = failed_manifest(tmp_path)
    runtime = tmp_path / "runtime"
    first_path, first = diagnose(manifest, runtime)
    second_path, second = diagnose(manifest, runtime)
    assert first_path == second_path
    assert first["auto_skillopt"]["entered"] is True
    assert first["auto_skillopt"]["eligible"] is True
    assert first["auto_skillopt"]["route"] == "public_rule_candidate"
    assert first["auto_skillopt"]["status"] == "queued"
    assert second["event_id"] == first["event_id"]
    assert "test failure" not in first_path.read_text(encoding="utf-8")
    assert (runtime / "diagnoses" / f"{first['event_id']}.json").is_file()


@pytest.mark.parametrize("code", ["EVIDENCE_GATE_BLOCKED", "BOTTOM_WHITESPACE_EXCESS", "PAGE_COUNT_ERROR", "NEEDS_USER_INPUT", "BUSINESS_READABILITY_ERROR"])
def test_all_failures_enter_controller_with_protected_route(tmp_path: Path, code: str):
    manifest = failed_manifest(tmp_path, code)
    event = build_event(manifest)
    assert event["auto_skillopt"]["entered"] is True
    assert event["auto_skillopt"]["eligible"] is False
    assert event["auto_skillopt"]["status"] == "queued"
    assert event["auto_skillopt"]["route"] in {"content_recovery", "evidence_review"}


def test_docx_delivery_failure_enters_public_rule_lane(tmp_path: Path):
    manifest = failed_manifest(tmp_path, "DOCX_DELIVERY_BLOCKED")
    event = build_event(manifest)
    assert event["auto_skillopt"]["entered"] is True
    assert event["auto_skillopt"]["eligible"] is True
    assert event["auto_skillopt"]["route"] == "public_rule_candidate"
    assert event["gate"] == "delivery_gate_blocked"


def test_content_failure_creates_recovery_request_and_cooldown(tmp_path: Path):
    manifest = failed_manifest(tmp_path, "BOTTOM_WHITESPACE_EXCESS")
    runtime = tmp_path / "runtime"
    _, event = diagnose(manifest, runtime)
    skill = tmp_path / "SKILL.md"
    skill.write_text("active", encoding="utf-8")
    result = run_optimizer(event, runtime, skill, "unused", None, False, 3600)
    assert result["auto_skillopt"]["status"] == "needs_user_input"
    request = Path(result["auto_skillopt"]["recovery_request"])
    payload = json.loads(request.read_text(encoding="utf-8"))
    assert payload["route"] == "content_recovery"
    assert "authorized private.yaml Claims" in " ".join(payload["constraints"])
    again = run_optimizer(result, runtime, skill, "unused", None, False, 3600)
    assert again["auto_skillopt"]["status"] == "cooldown"
    assert again["auto_skillopt"]["recovery_request"] == result["auto_skillopt"]["recovery_request"]
    assert skill.read_text(encoding="utf-8") == "active"


def test_content_failure_materializes_unused_claim_candidate(tmp_path: Path):
    manifest = failed_manifest(tmp_path, "BOTTOM_WHITESPACE_EXCESS")
    quarantine = manifest.parent
    (quarantine / "resume-plan.json").write_text(json.dumps({
        "projects": [{"id": "p1", "claims": [
            {"id": "c1", "kind": "context", "text": "业务背景尚未呈现"},
        ]}],
    }, ensure_ascii=False), encoding="utf-8")
    (quarantine / "typeset-plan.json").write_text(json.dumps({
        "projects": [{"id": "p1", "bullets": [{"text": "已使用其他内容"}]}],
    }, ensure_ascii=False), encoding="utf-8")
    runtime = tmp_path / "runtime"
    _, event = diagnose(manifest, runtime)
    skill = tmp_path / "SKILL.md"
    skill.write_text("active", encoding="utf-8")
    result = run_optimizer(event, runtime, skill, "", None, False, 3600)
    assert result["auto_skillopt"]["status"] == "candidate_ready"
    request = json.loads(Path(result["auto_skillopt"]["recovery_request"]).read_text(encoding="utf-8"))
    assert request["unused_claim_count"] == 1
    candidate = json.loads(Path(request["candidate_path"]).read_text(encoding="utf-8"))
    assert candidate["unused_authorized_claims"][0]["claim_id"] == "c1"


def test_sparse_failure_with_all_claims_used_still_materializes_recompose_candidate(tmp_path: Path):
    output = tmp_path / "output"
    run_id, staging = begin_render_transaction(output)
    (staging / "reflow-trace.json").write_text(json.dumps({
        "rounds": [{"page_count": 1, "bottom_whitespace_pt": 247,
                    "findings": [{"code": "BOTTOM_WHITESPACE_EXCESS"}]}],
    }), encoding="utf-8")
    claims = [
        {"id": "ctx", "kind": "context", "text": "岗位资料查询存在越权和遗漏风险需要按岗位隔离授权并保留复核轨迹"},
        {"id": "arch", "kind": "architecture", "text": "按岗位隔离授权并保留复核轨迹，配置检索权限确保资料只向对应岗位开放，并支持管理员审计追溯和异常复盘及回归验证"},
        {"id": "metric", "kind": "metric", "text": "离线评测中核心资料查找准确率高达97.5%"},
    ]
    (staging / "resume-plan.json").write_text(json.dumps({
        "projects": [{"id": "p1", "claims": claims}],
    }, ensure_ascii=False), encoding="utf-8")
    (staging / "typeset-plan.json").write_text(json.dumps({
        "projects": [{"id": "p1", "bullets": [{"text": c["text"]} for c in claims]}],
    }, ensure_ascii=False), encoding="utf-8")
    quarantine = quarantine_render_transaction(
        output, run_id, staging, code="BOTTOM_WHITESPACE_EXCESS", detail="sparse", phase="pdf_reflow",
    )
    runtime = tmp_path / "runtime"
    _, event = diagnose(quarantine / "failed-manifest.json", runtime)
    skill = tmp_path / "SKILL.md"
    skill.write_text("active", encoding="utf-8")
    result = run_optimizer(event, runtime, skill, "", None, False, 3600)
    assert result["auto_skillopt"]["status"] == "candidate_ready"
    request = json.loads(Path(result["auto_skillopt"]["recovery_request"]).read_text(encoding="utf-8"))
    assert request["unused_claim_count"] == 0
    assert Path(request["typeset_plan_candidate_path"]).is_file()


def test_content_recovery_is_not_requeued_after_render_attempt(tmp_path: Path):
    manifest = failed_manifest(tmp_path, "BOTTOM_WHITESPACE_EXCESS")
    quarantine = manifest.parent
    (quarantine / "content-recovery-trace.json").write_text(json.dumps({
        "status": "candidate_applied", "before_bottom_whitespace_pt": 247,
    }), encoding="utf-8")
    (quarantine / "resume-plan.json").write_text(json.dumps({
        "projects": [{"id": "p1", "claims": [{"id": "c1", "kind": "context", "text": "仍有授权材料"}]}],
    }, ensure_ascii=False), encoding="utf-8")
    (quarantine / "typeset-plan.json").write_text(json.dumps({
        "projects": [{"id": "p1", "bullets": [{"text": "已渲染候选"}]}],
    }, ensure_ascii=False), encoding="utf-8")
    runtime = tmp_path / "runtime"
    _, event = diagnose(manifest, runtime)
    skill = tmp_path / "SKILL.md"
    skill.write_text("active", encoding="utf-8")
    result = run_optimizer(event, runtime, skill, "", None, False, 3600)
    assert result["auto_skillopt"]["status"] == "needs_user_input"
    request = json.loads(Path(result["auto_skillopt"]["recovery_request"]).read_text(encoding="utf-8"))
    assert request["status"] == "needs_user_input"
    assert "rendered once" in " ".join(request["actions"])


def test_overfull_content_failure_proposes_whole_project_prune(tmp_path: Path):
    output = tmp_path / "output"
    run_id, staging = begin_render_transaction(output)
    (staging / "reflow-trace.json").write_text(json.dumps({
        "rounds": [{"page_count": 2, "bottom_whitespace_pt": 0, "findings": [{"code": "PAGE_COUNT_ERROR"}]}],
    }), encoding="utf-8")
    (staging / "resume-plan.json").write_text(json.dumps({
        "projects": [{"id": f"p{i}", "claims": []} for i in range(4)],
    }), encoding="utf-8")
    quarantine = quarantine_render_transaction(
        output, run_id, staging, code="CONTENT_GATE_BLOCKED", detail="overfull", phase="pdf_reflow",
    )
    manifest = quarantine / "failed-manifest.json"
    runtime = tmp_path / "runtime"
    _, event = diagnose(manifest, runtime)
    skill = tmp_path / "SKILL.md"
    skill.write_text("active", encoding="utf-8")
    result = run_optimizer(event, runtime, skill, "", None, False, 3600)
    assert result["auto_skillopt"]["status"] == "candidate_ready"
    request = json.loads(Path(result["auto_skillopt"]["recovery_request"]).read_text(encoding="utf-8"))
    candidate = json.loads(Path(request["candidate_path"]).read_text(encoding="utf-8"))
    assert candidate["mode"] == "prune_project"
    assert candidate["remove_project_id"] == "p3"


def test_failure_event_preserves_redacted_reflow_history(tmp_path: Path):
    output = tmp_path / "output"
    run_id, staging = begin_render_transaction(output)
    (staging / "reflow-trace.json").write_text(json.dumps({
        "rounds": [
            {"round": 0, "layout_state": "normal", "page_count": 2,
             "bottom_whitespace_pt": 700, "findings": [{"code": "PAGE_COUNT_ERROR"}]},
            {"round": 1, "layout_state": "compact_1", "page_count": 2,
             "bottom_whitespace_pt": 500, "findings": [{"code": "PAGE_COUNT_ERROR"}]},
        ],
    }), encoding="utf-8")
    quarantine = quarantine_render_transaction(
        output, run_id, staging, code="CONTENT_GATE_BLOCKED", detail="overfull", phase="pdf_reflow",
    )
    event = build_event(quarantine / "failed-manifest.json")
    history = event["measurements"]["round_history"]
    assert [(item["layout_state"], item["page_count"]) for item in history] == [
        ("normal", 2), ("compact_1", 2),
    ]


def test_optimizer_run_respects_cooldown_and_does_not_touch_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = failed_manifest(tmp_path)
    runtime = tmp_path / "runtime"
    _, event = diagnose(manifest, runtime)
    skill = tmp_path / "SKILL.md"
    skill.write_text("active", encoding="utf-8")
    calls = []

    class Completed:
        returncode = 2
        stdout = '{"decision":"rejected"}'
        stderr = ""

    monkeypatch.setattr("skillopt_auto_loop.subprocess.run", lambda *args, **kwargs: calls.append(args[0]) or Completed())
    result = run_optimizer(event, runtime, skill, "python benchmark.py", None, True, 3600)
    assert result["auto_skillopt"]["status"] == "candidate_rejected"
    assert calls and "skillopt_pipeline.py" in str(calls[0])
    assert skill.read_text(encoding="utf-8") == "active"
    second = run_optimizer(event, runtime, skill, "python benchmark.py", None, True, 3600)
    assert second["auto_skillopt"]["status"] == "cooldown"


def test_public_failure_without_benchmark_creates_pending_offline_candidate(tmp_path: Path):
    manifest = failed_manifest(tmp_path, "PARAGRAPH_SPACING_ERROR")
    runtime = tmp_path / "runtime"
    _, event = diagnose(manifest, runtime)
    skill = Path(__file__).parents[1] / "SKILL.md"
    incumbent = skill.read_text(encoding="utf-8")
    result = run_optimizer(event, runtime, skill, "", None, False, 3600)
    assert result["auto_skillopt"]["status"] == "candidate_pending_validation"
    candidate = Path(result["auto_skillopt"]["candidate_path"])
    assert candidate.is_file()
    assert "SkillOpt 候选规则" in candidate.read_text(encoding="utf-8")
    assert skill.read_text(encoding="utf-8") == incumbent


def test_rollback_removes_canary_pointer_and_records_incumbent(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    active = runtime / "active-SKILL.md"
    incumbent = runtime / "incumbent-SKILL.md"
    active.write_text("candidate", encoding="utf-8")
    incumbent.write_text("incumbent", encoding="utf-8")
    (runtime / "active_candidate.json").write_text(json.dumps({
        "active_candidate": str(active), "active_path": str(active),
        "incumbent_path": str(incumbent), "incumbent_sha256": "c" * 64,
    }), encoding="utf-8")
    record = rollback(runtime, "failure-1", "sentinel regression")
    assert not (runtime / "active_candidate.json").exists()
    assert active.read_text(encoding="utf-8") == "incumbent"
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["status"] == "rolled_back"
    assert payload["restored_incumbent"] == "c" * 64


def test_manifest_must_be_in_quarantine(tmp_path: Path):
    path = tmp_path / "failed-manifest.json"
    path.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
    with pytest.raises(AutoLoopError, match="quarantine"):
        build_event(path)


def test_bottom_whitespace_measurement_is_preserved_when_within_approved_band(tmp_path: Path):
    from build_resume import parse_geometry

    geometry = tmp_path / "geometry-qa.json"
    geometry.write_text(json.dumps({
        "passed": True,
        "bottom_whitespace_pt": 45.5,
        "findings": [],
        "qa_measurements": {},
    }), encoding="utf-8")
    parsed = parse_geometry(geometry)
    assert parsed["bottom_whitespace_pt"] == 45.5


def test_content_recovery_expands_only_within_frozen_budget():
    from skillopt_auto_loop import build_typeset_candidate, cjk_count

    claim_text = "岗位资料查询存在越权和遗漏风险，需要按岗位隔离授权并保留可解释复核轨迹"
    solution_text = "按岗位隔离授权并保留可解释复核轨迹，配置检索权限与复核流程，确保资料只向对应岗位开放，并支持管理员审计追溯"
    metric_text = "固定离线评测中各岗位核心资料查找准确率高达97.5%"
    plan = {"projects": [{"id": "p1", "claims": [
        {"id": "c1", "kind": "context", "text": claim_text, "allowed_for_resume": True},
        {"id": "c2", "kind": "architecture", "text": solution_text, "allowed_for_resume": True},
        {"id": "c3", "kind": "metric", "text": metric_text, "allowed_for_resume": True},
    ]}]}
    candidate = build_typeset_candidate(plan, None, content_mode="expanded")
    assert candidate is not None
    assert candidate["content_mode"] == "expanded"
    for bullet in candidate["projects"][0]["bullets"]:
        assert 50 <= cjk_count(bullet["text"]) <= 130
