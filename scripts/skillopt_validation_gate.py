#!/usr/bin/env python3
"""Run an evidence-preserving SkillOpt validation gate over JSON fixtures.

The renderer and QA checker are supplied by the host project.  Both commands
are command templates (not shell snippets) and may use these placeholders:

* ``{fixture}``    absolute path to one JSON fixture
* ``{skill}``      incumbent or temporary candidate SKILL.md
* ``{output_dir}`` per-fixture output directory
* ``{pdf}``        PDF reported by the renderer (QA command only)
* ``{docx}``       optional DOCX reported by the renderer (QA command only)

The renderer must emit exactly one JSON object on stdout:
``{"pdf": "/abs/resume.pdf", "docx": "/abs/resume.docx"}``; DOCX is optional
because Typst PDF is the authoritative layout artifact.
The QA command must emit ``{"findings": [{"code": "...", "severity":
"error"}]}``.  Paths in renderer output may instead be relative to
``{output_dir}``.
"""
from __future__ import annotations

import argparse
import json
import logging
import shlex
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from skillopt_pipeline import apply_bounded_patch


LOG = logging.getLogger("skillopt.validation_gate")
SAFETY_CODES = frozenset({"CLIPPING", "OVERLAP"})


class GateError(RuntimeError):
    """Raised for an invalid input, failed pipeline command, or rejected gate."""


@dataclass(frozen=True)
class RenderedArtifacts:
    pdf: Path
    docx: Path | None = None


@dataclass(frozen=True)
class FixtureResult:
    fixture: str
    findings: tuple[dict[str, str], ...]

    @property
    def error_codes(self) -> frozenset[str]:
        return frozenset(
            finding["code"]
            for finding in self.findings
            if finding["severity"].lower() == "error"
        )

    @property
    def passed(self) -> bool:
        return not self.error_codes


@dataclass(frozen=True)
class Score:
    total: int
    passed: int

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def load_patch(path: Path) -> list[dict[str, Any]]:
    """Load either a raw RFC 6902 list or SkillOpt's ``{"patch": [...]}`` envelope."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"Cannot read proposal JSON {path}: {exc}") from exc
    operations = payload.get("patch") if isinstance(payload, dict) else payload
    if not isinstance(operations, list) or not all(isinstance(item, dict) for item in operations):
        raise GateError("Proposal must be an RFC 6902 operation list or an object with a 'patch' list")
    if not operations:
        raise GateError("An empty optimizer patch cannot be promoted")
    return operations


def create_candidate(incumbent: Path, proposal: Path, candidate: Path) -> Path:
    """Apply the existing bounded-patch policy without changing the incumbent."""
    try:
        incumbent_text = incumbent.read_text(encoding="utf-8")
        candidate_text, _ = apply_bounded_patch(incumbent_text, load_patch(proposal))
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(candidate_text, encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise GateError(f"Candidate generation failed: {exc}") from exc
    LOG.info("Candidate skill written: %s", candidate)
    return candidate


def discover_fixtures(root: Path) -> list[Path]:
    if not root.is_dir():
        raise GateError(f"Fixture root is not a directory: {root}")
    fixtures = sorted(path.resolve() for path in root.rglob("*.json") if path.is_file())
    if not fixtures:
        raise GateError(f"No JSON fixtures found below {root}")
    return fixtures


def parse_json_stdout(completed: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    if completed.returncode:
        raise GateError(
            f"{label} exited {completed.returncode}: {completed.stderr.strip() or '<no stderr>'}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GateError(f"{label} did not emit valid JSON: {exc}; stdout={completed.stdout!r}") from exc
    if not isinstance(value, dict):
        raise GateError(f"{label} must emit a JSON object")
    return value


def run_template(template: str, values: dict[str, str], label: str) -> dict[str, Any]:
    try:
        command = [part.format(**values) for part in shlex.split(template)]
    except (KeyError, ValueError) as exc:
        raise GateError(f"Invalid {label} command template: {exc}") from exc
    if not command:
        raise GateError(f"{label} command is empty")
    LOG.debug("Running %s: %s", label, shlex.join(command))
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise GateError(f"Cannot start {label}: {exc}") from exc
    return parse_json_stdout(completed, label)


def artifact_path(value: Any, output_dir: Path, kind: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise GateError(f"Renderer response is missing a non-empty '{kind}' path")
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (output_dir / path).resolve()
    if not resolved.is_file():
        raise GateError(f"Renderer reported {kind} that does not exist: {resolved}")
    return resolved


def render_fixture(template: str, fixture: Path, skill: Path, output_dir: Path) -> RenderedArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    response = run_template(
        template,
        {"fixture": str(fixture), "skill": str(skill), "output_dir": str(output_dir), "pdf": "", "docx": ""},
        f"render[{fixture.name}]",
    )
    return RenderedArtifacts(
        pdf=artifact_path(response.get("pdf"), output_dir, "pdf"),
        docx=artifact_path(response.get("docx"), output_dir, "docx") if response.get("docx") else None,
    )


def qa_fixture(template: str, fixture: Path, skill: Path, output_dir: Path, artifacts: RenderedArtifacts) -> FixtureResult:
    response = run_template(
        template,
        {
            "fixture": str(fixture), "skill": str(skill), "output_dir": str(output_dir),
            "pdf": str(artifacts.pdf), "docx": str(artifacts.docx) if artifacts.docx else "",
        },
        f"qa[{fixture.name}]",
    )
    raw_findings = response.get("findings")
    if not isinstance(raw_findings, list):
        raise GateError(f"qa[{fixture.name}] response must contain a 'findings' list")
    findings: list[dict[str, str]] = []
    for index, finding in enumerate(raw_findings):
        if not isinstance(finding, dict) or not isinstance(finding.get("code"), str):
            raise GateError(f"qa[{fixture.name}] finding {index} needs a string 'code'")
        severity = finding.get("severity", "error")
        if not isinstance(severity, str):
            raise GateError(f"qa[{fixture.name}] finding {index} has invalid severity")
        findings.append({"code": finding["code"], "severity": severity})
    return FixtureResult(str(fixture), tuple(findings))


def evaluate(skill: Path, fixtures: Iterable[Path], render_command: str, qa_command: str, output_root: Path) -> list[FixtureResult]:
    results: list[FixtureResult] = []
    for index, fixture in enumerate(fixtures, start=1):
        fixture_dir = output_root / f"{index:03d}-{fixture.stem}"
        try:
            artifacts = render_fixture(render_command, fixture, skill, fixture_dir)
            result = qa_fixture(qa_command, fixture, skill, fixture_dir, artifacts)
        except GateError:
            LOG.exception("Fixture failed to execute: %s", fixture)
            raise
        results.append(result)
        LOG.info("%s %s", "PASS" if result.passed else "FAIL", fixture.name)
    return results


def score(results: list[FixtureResult]) -> Score:
    return Score(total=len(results), passed=sum(result.passed for result in results))


def new_safety_regressions(incumbent: list[FixtureResult], candidate: list[FixtureResult]) -> dict[str, list[str]]:
    baseline = {item.fixture: item.error_codes for item in incumbent}
    regressions: dict[str, list[str]] = {}
    for item in candidate:
        introduced = sorted((item.error_codes & SAFETY_CODES) - (baseline[item.fixture] & SAFETY_CODES))
        if introduced:
            regressions[item.fixture] = introduced
    return regressions


def decide(incumbent: list[FixtureResult], candidate: list[FixtureResult]) -> tuple[Score, Score]:
    old_score, new_score = score(incumbent), score(candidate)
    if old_score.total != new_score.total or old_score.total == 0:
        raise GateError("Incomplete benchmark: incumbent and candidate fixture totals differ")
    regressions = new_safety_regressions(incumbent, candidate)
    if regressions:
        raise GateError(f"Rejected: new safety errors introduced: {json.dumps(regressions, ensure_ascii=False)}")
    if new_score.pass_rate <= old_score.pass_rate:
        raise GateError(
            "Rejected: candidate A4 QA pass rate must be strictly greater "
            f"({new_score.pass_rate:.2%} <= {old_score.pass_rate:.2%})"
        )
    return old_score, new_score


def write_report(path: Path, incumbent: list[FixtureResult], candidate: list[FixtureResult], error: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "incumbent": {"score": asdict(score(incumbent)), "fixtures": [asdict(item) for item in incumbent]},
        "candidate": {"score": asdict(score(candidate)), "fixtures": [asdict(item) for item in candidate]},
        "error": error,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", type=Path, required=True, help="Active incumbent SKILL.md")
    parser.add_argument("--proposal", type=Path, required=True, help="Optimizer JSON Patch or envelope")
    parser.add_argument("--fixtures", type=Path, required=True, help="Directory recursively containing JSON fixtures")
    parser.add_argument("--render-command", required=True, help="Renderer command template; emits Typst PDF and optional DOCX JSON")
    parser.add_argument("--qa-command", required=True, help="A4 QA command template; emits findings JSON")
    parser.add_argument("--work-dir", type=Path, required=True, help="Private runtime directory")
    parser.add_argument("--candidate-path", type=Path, help="Defaults to <work-dir>/skill_candidate.md")
    parser.add_argument("--report", type=Path, help="Defaults to <work-dir>/validation_report.json")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)
    candidate_path = args.candidate_path or args.work_dir / "skill_candidate.md"
    report_path = args.report or args.work_dir / "validation_report.json"
    incumbent_results: list[FixtureResult] = []
    candidate_results: list[FixtureResult] = []
    try:
        skill = args.skill.resolve()
        if not skill.is_file():
            raise GateError(f"Incumbent skill does not exist: {skill}")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        candidate = create_candidate(skill, args.proposal.resolve(), candidate_path.resolve())
        fixtures = discover_fixtures(args.fixtures.resolve())
        LOG.info("Evaluating %d fixture(s) against incumbent", len(fixtures))
        incumbent_results = evaluate(skill, fixtures, args.render_command, args.qa_command, args.work_dir / "incumbent")
        LOG.info("Evaluating %d fixture(s) against candidate", len(fixtures))
        candidate_results = evaluate(candidate, fixtures, args.render_command, args.qa_command, args.work_dir / "candidate")
        old_score, new_score = decide(incumbent_results, candidate_results)
        write_report(report_path, incumbent_results, candidate_results)
        LOG.info("\033[32mVALIDATION PASSED — merge allowed (%d/%d -> %d/%d, %.2f%% -> %.2f%%)\033[0m",
                 old_score.passed, old_score.total, new_score.passed, new_score.total,
                 old_score.pass_rate * 100, new_score.pass_rate * 100)
        return 0
    except GateError as exc:
        LOG.error("\033[31mVALIDATION REJECTED — %s\033[0m", exc)
        try:
            write_report(report_path, incumbent_results, candidate_results, str(exc))
        except OSError as report_exc:
            LOG.error("Could not write failure report: %s", report_exc)
        return 2
    except Exception:
        LOG.exception("\033[31mVALIDATION ERROR — unexpected failure\033[0m")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
