from __future__ import annotations

import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from skillopt_pipeline import BenchmarkScore, apply_bounded_patch, markdown_to_sections, run_benchmark, split_frontmatter  # noqa: E402


def test_allowed_patch_resolves_localized_skill_headings():
    skill_path = Path(__file__).parents[1] / "SKILL.md"
    skill = skill_path.read_text(encoding="utf-8")
    _, body = split_frontmatter(skill)
    sections, _ = markdown_to_sections(body)
    updated_section = sections["工作流"] + "\n- 记录候选规则的验证结果。\n"
    candidate, _ = apply_bounded_patch(skill, [{
        "op": "replace", "path": "/sections/workflow", "value": updated_section,
    }])
    assert "记录候选规则的验证结果。" in candidate
    assert "## 证据与安全" in candidate
    assert "## SkillOpt" in candidate

def test_patch_cannot_target_protected_or_unknown_sections():
    skill = (Path(__file__).parents[1] / "SKILL.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="disallowed patch operation"):
        apply_bounded_patch(skill, [{
            "op": "replace", "path": "/sections/evidence", "value": "## 证据与安全\n",
        }])


def test_benchmark_command_rejects_shell_injection(tmp_path: Path):
    emitter = tmp_path / "emit_score.py"
    marker = tmp_path / "pwned"
    emitter.write_text(
        "import json; print(json.dumps({'total': 1, 'passed': 1, 'a4_qa_pass_rate': 1.0}))\n",
        encoding="utf-8",
    )
    command = f"{sys.executable} {emitter} ; touch {marker}"
    with pytest.raises(RuntimeError, match="shell metacharacters"):
        run_benchmark(command, Path(__file__).parents[1] / "SKILL.md", "adversarial")
    assert not marker.exists()


def test_benchmark_shell_free_command_accepts_json_score(tmp_path: Path):
    emitter = tmp_path / "emit_score.py"
    emitter.write_text(
        "import json; print(json.dumps({'total': 1, 'passed': 1, 'a4_qa_pass_rate': 1.0}))\n",
        encoding="utf-8",
    )
    score = run_benchmark(f"{sys.executable} {emitter}", Path(__file__).parents[1] / "SKILL.md", "safe")
    assert isinstance(score, BenchmarkScore)
    assert score.passed == score.total == 1
