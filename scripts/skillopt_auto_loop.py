#!/usr/bin/env python3
"""Automatic, bounded SkillOpt diagnosis and candidate evaluation.

The loop consumes only quarantined failure metadata.  It may create a
candidate or recovery request in a private runtime, but it never edits the
active SKILL.md.  A caller enables automatic dispatch by providing the private
runtime and active Skill paths; ``SKILLOPT_AUTO_ENABLED=0`` disables it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from route_contract import metric_tokens_for_text, result_kinds_for_text


OPTIMIZABLE_CODES = frozenset({
    "PARAGRAPH_SPACING_ERROR", "PAGE_SIZE_ERROR", "MARGIN_OUT_OF_RANGE_ERROR",
    "MULTI_COLUMN_LAYOUT_ERROR", "VISUAL_DESIGN_MISMATCH_ERROR",
    "DELIVERY_GATE_BLOCKED", "DOCX_DELIVERY_BLOCKED",
})
NON_OPTIMIZABLE_CODES = frozenset({
    "EVIDENCE_GATE_BLOCKED", "INSUFFICIENT_PROJECT_EVIDENCE", "BULLET_LENGTH_ERROR",
    "BOTTOM_WHITESPACE_EXCESS", "PAGE_COUNT_ERROR", "CONTENT_GATE_BLOCKED", "NEEDS_USER_INPUT",
    "BUSINESS_CONTEXT_MISSING", "BUSINESS_ACTION_MISSING", "BUSINESS_RESULT_MISSING",
    "TECHNICAL_TERM_OVERLOAD", "TECHNICAL_TERM_PLACEMENT_ERROR", "BUSINESS_READABILITY_ERROR",
})
CONTENT_CODES = NON_OPTIMIZABLE_CODES
DEFAULT_COOLDOWN_SECONDS = 3600
CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
NUMBER_RE = re.compile(r"[0-9０-９]+(?:[.,．][0-9０-９]+)?[%％]?")
TECHNICAL_TERM_RE = re.compile(
    r"(?i)(?:Python|FastAPI|Django|Flask|Pydantic|LangChain|LangGraph|LlamaIndex|OpenClaw|RAG|Agent|API|Cron|ACL|JWT|JSON|BM25|Reranker|StrictBool|StrictInt|Decimal|Schema|Patch|Worker|Turnstile|MCP|LoRA|向量数据库|向量|模型|算法|架构|重构|检索|召回|鉴权|序列化|异步|接口|编排|调度)"
)
MAX_TECHNICAL_TERMS_PER_BULLET = 2

OFFLINE_RULE_HINTS = {
    "PARAGRAPH_SPACING_ERROR": (
        "workflow",
        "渲染完成后必须读取 PDF 与 OOXML 的实际行距、段后距和字号；任一实测值偏离冻结契约时阻断交付并记录测量值。",
    ),
    "PAGE_SIZE_ERROR": (
        "one-page-layout-qa",
        "提交前必须从 PDF mediabox 与 DOCX section properties 实测 A4 尺寸，不能只相信模板声明。",
    ),
    "MARGIN_OUT_OF_RANGE_ERROR": (
        "one-page-layout-qa",
        "提交前必须实测四边页边距并记录数值；超出白名单时隔离产物，不得靠缩小字体掩盖。",
    ),
    "MULTI_COLUMN_LAYOUT_ERROR": (
        "one-page-layout-qa",
        "提交前必须实测正文单列阅读顺序；检测到正文多栏时直接阻断，不得自动改写内容。",
    ),
    "VISUAL_DESIGN_MISMATCH_ERROR": (
        "output-routing",
        "交付前必须核对批准的 theme variant、线条和标题标记均已落到 PDF 与 DOCX。",
    ),
    "DELIVERY_GATE_BLOCKED": (
        "output-routing",
        "任何交付门控失败都必须保留失败测量、隔离产物并重新运行完整 PDF/DOCX QA。",
    ),
    "DOCX_DELIVERY_BLOCKED": (
        "output-routing",
        "DOCX 交付失败时只能隔离 DOCX 并记录实际渲染原因，不能覆盖已合格的 PDF。",
    ),
}


class AutoLoopError(RuntimeError):
    """A deterministic failure in the automatic SkillOpt controller."""


def cjk_count(text: str) -> int:
    return len(CJK_RE.findall(text))


def technical_terms(text: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for match in TECHNICAL_TERM_RE.finditer(text):
        key = match.group(0).lower()
        if key not in seen:
            seen.add(key)
            terms.append(match.group(0))
    return terms


def technical_term_positions(text: str) -> list[tuple[str, int, int]]:
    return [(match.group(0), match.start(), match.end()) for match in TECHNICAL_TERM_RE.finditer(text)]


def technical_safe_segments(text: str) -> list[str]:
    """Return contiguous source slices with technical spans removed."""
    positions = technical_term_positions(text)
    if not positions:
        return [text]
    boundaries = [0]
    for _, start, end in positions:
        boundaries.extend((start, end))
    boundaries.append(len(text))
    segments: list[str] = []
    for left, right in zip(boundaries[::2], boundaries[1::2]):
        fragment = text[left:right].strip("，。；、 ")
        if fragment:
            segments.append(fragment)
    return segments


def take_cjk_prefix(text: str, limit: int) -> str:
    """Take a verbatim prefix without exceeding the CJK budget."""
    if limit <= 0:
        return ""
    count = 0
    end = 0
    for index, char in enumerate(text):
        if CJK_RE.match(char):
            if count >= limit:
                break
            count += 1
        end = index + 1
    return text[:end]


def take_cjk_suffix(text: str, limit: int) -> str:
    """Take a verbatim suffix without exceeding the CJK budget."""
    if limit <= 0:
        return ""
    positions = [index for index, char in enumerate(text) if CJK_RE.match(char)]
    if len(positions) <= limit:
        return text
    return text[positions[-limit]:]


def compose_stage(
    claims: list[dict[str, Any]], kinds: set[str], *, result: bool = False,
    min_chars: int = 40, max_chars: int = 50,
) -> dict[str, Any] | None:
    """Compose a bounded CJK stage solely from verbatim Claim fragments.

    This is a conservative fallback for sparse pages.  It never invents
    connective prose: every non-punctuation character in the output is copied
    from one listed Claim, so the normal Agent B evidence gate can re-check it.
    """
    selected = [c for c in claims if c.get("kind") in kinds and c.get("text")]
    if not selected:
        return None
    # Use the upper edge of the selected frozen budget.  Recovery exists to
    # correct page density, so leaving a candidate at the midpoint would
    # preserve the original sparse/overfull failure unnecessarily.
    # The expanded contract allows up to 130 CJK characters. The first sparse
    # recovery uses a moderate target; a later recovery may add only one more
    # bounded increment. Explicitly authored 50–130 candidates remain valid.
    target = max_chars
    fragments: list[tuple[str, str]] = []
    if result:
        metrics = [c for c in selected if c.get("kind") == "metric"]
        # A result bullet is metric-gated as a whole: any Arabic/full-width
        # number or percent sign in it must be supported by the terminal
        # metric Claim.  Do not pull numeric fragments such as “4 类信息源”
        # from an architecture Claim into the result prefix, otherwise the
        # evidence gate quite correctly rejects the candidate as a mixed-kind
        # metric assertion.
        # Keep the final result Claim at the end. A metric Claim is preferred;
        # when no metric exists, an authorized delivery/control/architecture
        # Claim is a valid bounded terminal conclusion.
        if metrics:
            terminal = metrics[-1]
        else:
            non_numeric = [
                c for c in selected
                if not metric_tokens_for_text(str(c.get("text", "")), numeric_re=NUMBER_RE)
                and not any(term in str(c.get("text", "")) for term in ("提升", "降低"))
            ]
            # Prefer a control conclusion because delivery Claims may contain
            # technical vocabulary such as "架构", which is forbidden in the
            # project result stage.
            priority = {"control": 0, "delivery": 1, "architecture": 2}
            terminal = min(
                non_numeric,
                key=lambda c: (priority.get(str(c.get("kind")), 9), -cjk_count(str(c.get("text", "")))),
            ) if non_numeric else None
        if terminal is None:
            return None
        other: list[dict[str, Any]] = []
        for claim in claims:
            if claim.get("kind") == "metric" or not claim.get("text") or claim.get("id") == terminal.get("id"):
                continue
            claim_text = str(claim["text"])
            if metric_tokens_for_text(claim_text, numeric_re=NUMBER_RE):
                continue
            # Result bullets cannot expose technical terms. Preserve a
            # verbatim prefix before the first such term so an otherwise
            # valid action/delivery Claim can still provide bounded length.
            positions = technical_term_positions(claim_text)
            safe_text = claim_text[:positions[0][1]].rstrip("，。；、 ") if positions else claim_text
            if cjk_count(safe_text) >= 8:
                other.append({**claim, "text": safe_text})
        # Prefer one complete delivery/control statement before the metric.
        # Concatenating several unrelated prefixes creates semantically odd
        # text and can also make PDF glyph-order verification ambiguous.
        terminal_text = str(terminal["text"])
        positions = technical_term_positions(terminal_text)
        if terminal.get("kind") == "metric":
            # A metric Claim may contain a trailing architecture clause. Keep
            # the metric-bearing prefix as the result and leave technical
            # vocabulary to the solution stage.
            suffix = terminal_text[positions[-1][2]:].strip("，。；、 ") if positions else ""
            suffix_selected = bool(suffix and metric_tokens_for_text(suffix, numeric_re=NUMBER_RE) and not technical_terms(suffix))
            if suffix_selected:
                terminal_text = suffix
            if not suffix_selected:
                safe_metric_segments = [
                    segment for segment in technical_safe_segments(terminal_text)
                    if metric_tokens_for_text(segment, numeric_re=NUMBER_RE)
                ]
                if safe_metric_segments:
                    terminal_text = max(safe_metric_segments, key=cjk_count)
                else:
                    for match in positions:
                        prefix = terminal_text[:match[1]].rstrip("，。；、 ")
                        if prefix and metric_tokens_for_text(prefix, numeric_re=NUMBER_RE):
                            terminal_text = prefix
                            break
        elif positions:
            # Qualitative terminal conclusions must also keep technical terms
            # in the solution stage; retain a source-verbatim prefix instead.
            terminal_text = terminal_text[:positions[0][1]].rstrip("，。；、 ")
        terminal_count = cjk_count(terminal_text)
        if terminal_count > max_chars:
            # Compression may shorten a long metric Claim, but only by taking
            # a verbatim suffix from that same Claim.  Keeping the suffix
            # preserves the terminal number in the common source format
            # ("...准确率97.5%") while never inventing a new result phrase.
            terminal_text = take_cjk_suffix(terminal_text, max_chars)
            terminal_count = cjk_count(terminal_text)
            if terminal.get("kind") == "metric" and not metric_tokens_for_text(terminal_text, numeric_re=NUMBER_RE):
                return None
        wanted_prefix = target - terminal_count
        sorted_other = sorted(other, key=lambda c: (0 if c.get("kind") == "delivery" else 1, -cjk_count(str(c.get("text", "")))))
        for claim in sorted_other:
            claim_text = str(claim["text"])
            claim_count = cjk_count(claim_text)
            remaining = wanted_prefix - cjk_count("".join(f for f, _ in fragments))
            if claim_count <= remaining and claim_count >= 8:
                fragments.append((claim_text, str(claim["id"])))
                continue
            # Do not treat a tiny 11–20-character context as the whole
            # pre-result clause.  It is safe as an additional fragment when
            # combined with other complete, source-backed clauses.
            if not fragments and claim_count < max(min_chars - 20, 20):
                continue
            if remaining > 0:
                fragment = take_cjk_prefix(claim_text, remaining)
                if fragment and cjk_count(fragment) >= max(1, min_chars - terminal_count - cjk_count("".join(f for f, _ in fragments))):
                    fragments.append((fragment, str(claim["id"])))
            if cjk_count("".join(f for f, _ in fragments)) >= wanted_prefix:
                break
        if not fragments:
            # Fall back to a single verbatim prefix only when a complete
            # statement cannot reach the target.  The prefix remains directly
            # traceable to one Claim and is never assembled from fragments.
            for claim in sorted(other, key=lambda c: -cjk_count(str(c.get("text", "")))):
                fragment = take_cjk_prefix(str(claim["text"]), wanted_prefix)
                if fragment and cjk_count(fragment) >= max(1, wanted_prefix - 6):
                    fragments.append((fragment, str(claim["id"])))
                    break
        fragments.append((terminal_text, str(terminal["id"])))
    else:
        # Sparse context Claims (often small side projects) may be safely
        # paired with a non-numeric implementation fragment while retaining a
        # required context Claim.  This makes the fallback useful without
        # inventing connective wording.
        safe_context_count = sum(
            cjk_count(
                str(c["text"])[:technical_term_positions(str(c["text"]))[0][1]].rstrip("，。；、 ")
                if technical_term_positions(str(c["text"])) else str(c["text"])
            )
            for c in selected
        )
        if kinds == {"context"} and safe_context_count < min_chars:
            selected = selected + [c for c in claims if c not in selected and c.get("text")]
        # Use a complete Claim whenever it already satisfies the hard gate.
        # This is both more readable and safer than cutting a sentence in the
        # middle merely to hit an arbitrary character target.
        if kinds == {"context"}:
            # A background must actually begin with the business context; a
            # complete delivery/control Claim is not an acceptable substitute
            # merely because it happens to have the right length.  Context
            # Claims occasionally end with a technical review noun; those
            # Claims must take the safe prefix path below so the noun remains
            # in the solution stage.
            complete = [
                c for c in selected
                if min_chars <= cjk_count(str(c["text"])) <= max_chars
                if c.get("kind") == "context" and not technical_terms(str(c["text"]))
            ]
            if any(technical_terms(str(c["text"])) for c in selected if c.get("kind") == "context"):
                complete = []
        else:
            complete = [c for c in selected if min_chars <= cjk_count(str(c["text"])) <= max_chars]
        if complete:
            if kinds == {"context"}:
                kind_priority = {"context": 0, "architecture": 1, "control": 2, "delivery": 3}
            else:
                # Control/delivery Claims are usually already business-safe;
                # prefer them when an architecture Claim would exceed the
                # frozen two-term solution density cap.
                kind_priority = {"control": 0, "delivery": 1, "architecture": 2, "context": 3}
            claim = min(complete, key=lambda c: (
                kind_priority.get(str(c.get("kind")), 9),
                abs(cjk_count(str(c["text"])) - target),
            ))
            fragments.append((str(claim["text"]), str(claim["id"])))
        else:
            # Context should lead with the business situation; solution should
            # lead with an implementation/control Claim.  Only then do we
            # take a short verbatim prefix from a second Claim to reach 40–50.
            non_context_priority = {"control": 0, "delivery": 1, "architecture": 2}
            ordered = sorted(
                selected,
                key=lambda c: (
                    -1 if (kinds == {"context"} and c.get("kind") == "context") else non_context_priority.get(str(c.get("kind")), 3),
                    -cjk_count(str(c.get("text", ""))),
                ),
            )
            for claim in ordered:
                remaining = target - cjk_count("".join(f for f, _ in fragments))
                claim_text = str(claim["text"])
                if kinds == {"context"}:
                    positions = technical_term_positions(claim_text)
                    if positions:
                        claim_text = claim_text[:positions[0][1]].rstrip("，。；、 ")
                elif not result and technical_terms(claim_text):
                    # When short Claims force a solution to use a second
                    # source, keep the business-safe prefix of a technical
                    # architecture Claim.  The control/delivery Claim already
                    # carries the action; this avoids technical-term overload
                    # without inventing a connective sentence.
                    remaining_before_claim = remaining
                    for safe_segment in technical_safe_segments(claim_text):
                        if remaining_before_claim <= 0:
                            break
                        fragment = take_cjk_prefix(safe_segment, remaining_before_claim)
                        if fragment:
                            fragments.append((fragment, str(claim["id"])))
                            remaining_before_claim -= cjk_count(fragment)
                        if cjk_count("".join(f for f, _ in fragments)) >= min_chars:
                            break
                    if cjk_count("".join(f for f, _ in fragments)) >= min_chars:
                        break
                    continue
                fragment = take_cjk_prefix(claim_text, remaining)
                if fragment:
                    fragments.append((fragment, str(claim["id"])))
                if cjk_count("".join(f for f, _ in fragments)) >= min_chars:
                    break
    text = "；".join(fragment for fragment, _ in fragments)
    count = cjk_count(text)
    if not min_chars <= count <= max_chars:
        return None
    if kinds != {"context"} and not result and len(technical_terms(text)) > MAX_TECHNICAL_TERMS_PER_BULLET:
        # The recovery composer may use technical Claims only for the
        # solution stage, and never allows an unbounded stack of framework or
        # protocol names in one bullet.
        return None
    assertions = [{"text": fragment, "source_claim_id": claim_id} for fragment, claim_id in fragments]
    first_fragment = fragments[0][0]
    if result:
        terminal_phrase = fragments[-1][0]
        # A metric result must visibly retain a number/percentage in the
        # terminal phrase. Non-metric results intentionally remain qualitative
        # and are checked against architecture/control/delivery Claim kinds.
        if terminal.get("kind") == "metric" and not metric_tokens_for_text(terminal_phrase, numeric_re=NUMBER_RE):
            return None
        return {
            "text": text,
            "bold_phrases_used": [terminal_phrase],
            "terminal_bold_phrase": terminal_phrase,
            "source_claim_ids": list(dict.fromkeys(claim_id for _, claim_id in fragments)),
            "assertions": assertions,
        }
    return {
        "text": text,
        "bold_phrases_used": [first_fragment],
        "terminal_bold_phrase": None,
        "source_claim_ids": list(dict.fromkeys(claim_id for _, claim_id in fragments)),
        "assertions": assertions,
    }


def build_typeset_candidate(
    plan: dict[str, Any], existing_typeset: dict[str, Any] | None = None,
    *, content_mode: str | None = None, expanded_target_chars: int | None = None,
) -> dict[str, Any] | None:
    content_mode = content_mode or str((existing_typeset or {}).get("content_mode", "normal"))
    bounds = {"normal": (40, 50), "compressed": (30, 40), "expanded": (50, 130)}
    if content_mode not in bounds:
        return None
    min_chars, max_chars = bounds[content_mode]
    expanded_max_chars = max_chars
    if content_mode == "expanded":
        expanded_max_chars = max(50, min(120, expanded_target_chars or 80))
        if str((existing_typeset or {}).get("content_mode", "")) == "expanded":
            existing_lengths = [
                cjk_count(str(bullet.get("text", "")))
                for project in (existing_typeset or {}).get("projects", [])
                if isinstance(project, dict)
                for bullet in project.get("bullets", [])
                if isinstance(bullet, dict)
            ]
            if existing_lengths:
                expanded_max_chars = min(120, max(existing_lengths) + 15)
    existing_by_id = {
        str(project.get("id")): project
        for project in (existing_typeset or {}).get("projects", [])
        if isinstance(project, dict) and project.get("id")
    }
    projects: list[dict[str, Any]] = []
    for project in plan.get("projects", []):
        if not isinstance(project, dict):
            continue
        project_id = str(project.get("id", ""))
        # A sparse-page recovery must not rewrite already valid prose.  Keep
        # incumbent projects byte-for-byte and only compose stages for a newly
        # selected project.  This avoids turning a content recovery into a
        # noisy PDF text-order failure.
        if project_id in existing_by_id and content_mode == "normal":
            projects.append(existing_by_id[project_id])
            continue
        claims = [c for c in project.get("claims", []) if isinstance(c, dict) and c.get("allowed_for_resume", True)]
        has_usable_metric = any(
            c.get("kind") == "metric"
            and result_kinds_for_text(
                str(c.get("text", "")), numeric_re=NUMBER_RE,
                effect_terms=("提升", "降低"),
            ) == {"metric"}
            for c in claims
        )
        result_kinds = {"metric"} if has_usable_metric else {"architecture", "control", "delivery"}
        stage_max_chars = expanded_max_chars if content_mode == "expanded" else max_chars
        stages = [
            compose_stage(claims, {"context"}, min_chars=min_chars, max_chars=stage_max_chars),
            compose_stage(claims, {"architecture", "control", "delivery"}, min_chars=min_chars, max_chars=stage_max_chars),
            compose_stage(claims, result_kinds, result=True, min_chars=min_chars, max_chars=stage_max_chars),
        ]
        if any(stage is None for stage in stages):
            return None
        projects.append({"id": project_id, "overview": None,
                         "bullets": [{"stage": stage_name, **stage} for stage_name, stage in zip(("background", "solution", "result"), stages)]})
    return {"projects": projects, "employment": (existing_typeset or {}).get("employment", []), "content_mode": content_mode}


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutoLoopError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AutoLoopError(f"expected a JSON object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def failure_code(failed_manifest: dict[str, Any]) -> str:
    error = failed_manifest.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    return "UNKNOWN_GATE_ERROR"


def is_eligible(code: str, phase: str) -> bool:
    if code in NON_OPTIMIZABLE_CODES:
        return False
    if code in {"DELIVERY_GATE_BLOCKED", "DOCX_DELIVERY_BLOCKED"}:
        return phase in {"docx_delivery", "typst_delivery"}
    if code in OPTIMIZABLE_CODES:
        return True
    return False


def route_for(code: str, phase: str) -> str:
    """Return the bounded repair lane used after every failure.

    Every lane enters this controller.  Only ``public_rule_candidate`` may
    change SKILL.md sections; content/evidence lanes create an auditable
    recovery request instead of weakening facts or authorization rules.
    """
    if is_eligible(code, phase):
        return "public_rule_candidate"
    if code in CONTENT_CODES:
        return "content_recovery"
    if code.startswith("EVIDENCE") or "CLAIM" in code or "SOURCE" in code:
        return "evidence_review"
    return "diagnose_only"


def failure_signature(event: dict[str, Any]) -> str:
    payload = {
        "code": event.get("error_code"), "phase": event.get("phase"),
        "skill_sha256": event.get("skill_sha256"), "input_hashes": event.get("input_hashes", {}),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_failed_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AutoLoopError(f"failed manifest does not exist: {path}")
    if "quarantine" not in path.parts:
        raise AutoLoopError("failed manifest must be inside a quarantine directory")
    payload = read_json(path)
    if payload.get("status") != "failed":
        raise AutoLoopError("failed manifest status must be 'failed'")
    return payload


def build_event(failed_manifest_path: Path) -> dict[str, Any]:
    manifest = load_failed_manifest(failed_manifest_path)
    code = failure_code(manifest)
    phase = str(manifest.get("phase") or "unknown")
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), dict) else {}
    trace_path = failed_manifest_path.parent / "reflow-trace.json"
    redacted_inputs = {str(key): str(value) for key, value in inputs.items() if isinstance(value, str)}
    measurements: dict[str, Any] = {}
    if trace_path.is_file():
        try:
            trace = read_json(trace_path)
            rounds = trace.get("rounds")
            if isinstance(rounds, list) and rounds and isinstance(rounds[-1], dict):
                latest = rounds[-1]
                measurements = {
                    "page_count": latest.get("page_count"),
                    "bottom_whitespace_pt": latest.get("bottom_whitespace_pt"),
                    "finding_codes": [f.get("code") for f in latest.get("findings", []) if isinstance(f, dict)],
                    "qa_measurements": latest.get("qa_measurements", {}),
                    "round_history": [
                        {
                            "round": item.get("round"),
                            "layout_state": item.get("layout_state"),
                            "page_count": item.get("page_count"),
                            "bottom_whitespace_pt": item.get("bottom_whitespace_pt"),
                            "finding_codes": [
                                finding.get("code")
                                for finding in item.get("findings", [])
                                if isinstance(finding, dict) and isinstance(finding.get("code"), str)
                            ],
                        }
                        for item in rounds
                        if isinstance(item, dict)
                    ],
                }
                layout_hash = rounds[-1].get("layout_vars_sha256")
                if isinstance(layout_hash, str):
                    redacted_inputs["layout_vars_sha256"] = layout_hash
        except AutoLoopError:
            pass
    gate = "needs_user_input" if code in NON_OPTIMIZABLE_CODES else (
        "blocked" if code in {"DELIVERY_GATE_BLOCKED", "DOCX_DELIVERY_BLOCKED"} else "blocked"
    )
    eligible = is_eligible(code, phase)
    event = {
        "event_id": f"failure-{manifest.get('run_id') or uuid.uuid4().hex}",
        "run_id": manifest.get("run_id"),
        "gate": gate,
        "error_code": code,
        "phase": phase,
        "trace_path": str(trace_path) if trace_path.is_file() else None,
        "failed_manifest": str(failed_manifest_path),
        "input_hashes": redacted_inputs,
        "measurements": measurements,
        "skill_sha256": str(inputs.get("skill_sha256") or ""),
        "auto_skillopt": {
            "entered": True,
            "eligible": eligible,
            "route": route_for(code, phase),
            "status": "queued",
        },
        "created_at": now_iso(),
    }
    event["failure_signature"] = failure_signature(event)
    return event


def diagnose(failed_manifest_path: Path, runtime_root: Path) -> tuple[Path, dict[str, Any]]:
    event = build_event(failed_manifest_path)
    events_dir = runtime_root / "events"
    event_path = events_dir / f"{event['event_id']}.json"
    if event_path.is_file():
        event = read_json(event_path)
    else:
        atomic_write(event_path, event)
    diagnosis_path = runtime_root / "diagnoses" / f"{event['event_id']}.json"
    if not diagnosis_path.is_file():
        diagnosis = {
            "event_id": event["event_id"],
            "failure_signature": event["failure_signature"],
            "error_code": event["error_code"],
            "phase": event["phase"],
            "eligible": event["auto_skillopt"]["eligible"],
            "root_cause": {
                "category": event["auto_skillopt"]["route"],
                "source": "deterministic_gate_error_code",
                "action": "generate_bounded_candidate" if event["auto_skillopt"]["eligible"] else (
                    "generate_content_recovery_request" if event["auto_skillopt"]["route"] == "content_recovery"
                    else "request_human_review"
                ),
            },
            "redacted_observations": {
                "error_code": event["error_code"],
                "phase": event["phase"],
                "trace_path": event["trace_path"],
                "input_hashes": event["input_hashes"],
                "measurements": event.get("measurements", {}),
            },
            "created_at": now_iso(),
        }
        atomic_write(diagnosis_path, diagnosis)
    return event_path, event


def materialize_trajectory(event: dict[str, Any], runtime_root: Path) -> Path:
    """Create a metadata-only trajectory suitable for SkillOpt's loader."""
    trajectory_dir = runtime_root / "rollouts" / event["event_id"]
    trajectory_path = trajectory_dir / "trajectory.json"
    if trajectory_path.is_file():
        return trajectory_path
    findings = [{"code": event["error_code"], "severity": "error", "page": None, "message": None}]
    record = {
        "schema_version": "1.0", "event_id": event["event_id"], "timestamp": event["created_at"],
        "event_type": "a4_qa_failed",
        "run": {"run_id": event.get("run_id"), "input_hashes": event.get("input_hashes", {})},
        "qa": {"profile": "redacted", "passed": False, "findings": findings},
    }
    atomic_write(trajectory_path, record)
    return trajectory_path


def cooldown_path(runtime_root: Path, signature: str) -> Path:
    return runtime_root / "cooldown" / f"{signature}.json"


def in_cooldown(runtime_root: Path, signature: str, cooldown_seconds: int) -> bool:
    path = cooldown_path(runtime_root, signature)
    if not path.is_file():
        return False
    payload = read_json(path)
    try:
        until = datetime.fromisoformat(str(payload["cooldown_until"]))
    except (KeyError, ValueError) as exc:
        raise AutoLoopError(f"invalid cooldown record: {path}") from exc
    return datetime.now(UTC) < until


def write_cooldown(runtime_root: Path, event: dict[str, Any], cooldown_seconds: int, status: str) -> None:
    until = datetime.now(UTC) + timedelta(seconds=cooldown_seconds)
    atomic_write(cooldown_path(runtime_root, event["failure_signature"]), {
        "failure_signature": event["failure_signature"], "error_code": event["error_code"],
        "phase": event["phase"], "last_status": status, "cooldown_until": until.isoformat(),
    })


def write_content_recovery_request(runtime_root: Path, event: dict[str, Any]) -> Path:
    """Create a safe next-step proposal for sparse/content failures.

    This is intentionally a request, not an invented resume.  A later Agent B
    run may use the user's approved Claim set to recompose or add material;
    absent such evidence the only valid outcome is a precise user question.
    """
    route = event["auto_skillopt"].get("route")
    candidate_path: Path | None = None
    unused_claims: list[dict[str, str]] = []
    plan: dict[str, Any] = {}
    typeset: dict[str, Any] = {}
    typeset_candidate: dict[str, Any] | None = None
    quarantine_dir = Path(str(event.get("failed_manifest", ""))).parent
    if route == "content_recovery":
        # These files are already inside the private quarantine.  They are
        # never sent to the public-rule Optimizer; they only seed a local,
        # evidence-preserving Agent B rewrite candidate.
        try:
            plan = read_json(quarantine_dir / "resume-plan.json")
            typeset = read_json(quarantine_dir / "typeset-plan.json")
            rendered = {
                str(project.get("id")): " ".join(
                    str(bullet.get("text", ""))
                    for bullet in project.get("bullets", [])
                    if isinstance(bullet, dict)
                )
                for project in typeset.get("projects", [])
                if isinstance(project, dict)
            }
            for project in plan.get("projects", []):
                if not isinstance(project, dict):
                    continue
                project_id = str(project.get("id", ""))
                for claim in project.get("claims", []):
                    if not isinstance(claim, dict) or not claim.get("text"):
                        continue
                    text_value = str(claim["text"])
                    if text_value not in rendered.get(project_id, ""):
                        unused_claims.append({
                            "project_id": project_id,
                            "claim_id": str(claim.get("id", "")),
                            "kind": str(claim.get("kind", "")),
                            "text": text_value,
                        })
            typeset_candidate = build_typeset_candidate(plan, typeset, content_mode="expanded")
        except (AutoLoopError, OSError, json.JSONDecodeError, TypeError):
            unused_claims = []
        page_count = event.get("measurements", {}).get("page_count")
        # A build may already have consumed the one automatic recovery
        # attempt.  Do not label the same deterministic candidate as a fresh
        # solution (which would create an endless SkillOpt loop); surface the
        # remaining choice to the user instead.
        recovery_already_applied = (quarantine_dir / "content-recovery-trace.json").is_file()
        sparse_recompose_available = (
            isinstance(page_count, int)
            and page_count == 1
            and float(event.get("measurements", {}).get("bottom_whitespace_pt") or 0) > 50
            and typeset_candidate is not None
            and typeset_candidate != typeset
        )
        if (unused_claims or sparse_recompose_available) and not recovery_already_applied:
            candidate_path = runtime_root / "content-candidates" / f"{event['event_id']}.json"
            candidate_payload = {
                "event_id": event["event_id"],
                "mode": "bounded_recompose",
                "status": "candidate_ready",
                "unused_authorized_claims": unused_claims,
                "constraints": [
                    "use only the listed Claim text and IDs",
                    "keep exactly background, solution, result bullets per project",
                    "rerun all content, evidence, PDF and DOCX gates",
                ],
                "created_at": now_iso(),
            }
            if typeset_candidate is not None:
                candidate_payload["typeset_plan_candidate"] = typeset_candidate
            atomic_write(candidate_path, candidate_payload)
            if typeset_candidate is not None:
                typeset_candidate_path = candidate_path.with_name("typeset-plan-candidate.json")
                atomic_write(typeset_candidate_path, typeset_candidate)
        elif isinstance(page_count, int) and page_count > 1 and not recovery_already_applied:
            # Overfull content is also a content-recovery problem.  When the
            # plan has more than the minimum three projects, propose pruning
            # the lowest-ranked (last) project; never delete bullet text.
            try:
                plan_projects = plan.get("projects", [])
                if len(plan_projects) > 3 and isinstance(plan_projects[-1], dict):
                    candidate_path = runtime_root / "content-candidates" / f"{event['event_id']}-prune.json"
                    atomic_write(candidate_path, {
                        "event_id": event["event_id"],
                        "mode": "prune_project",
                        "status": "candidate_ready",
                        "remove_project_id": str(plan_projects[-1].get("id", "")),
                        "reason": "compact_2 remains over one A4 page",
                        "constraints": [
                            "remove one whole lowest-ranked project only",
                            "never delete or rewrite facts inside retained projects",
                            "rerun Agent A/B and all PDF/DOCX gates",
                        ],
                        "created_at": now_iso(),
                    })
            except (TypeError, OSError):
                candidate_path = None
        actions = [
            "inspect unused, same-project, allowed_for_resume Claims",
            "if sufficient Claims exist, propose a bounded Agent B recompose",
            "otherwise ask the user for an additional authorized project or Claim",
        ]
        if recovery_already_applied:
            actions = [
                "automatic evidence-only recovery was rendered once",
                "the page is still outside the 50pt density bound",
                "ask the user to authorize one more relevant project/Claim or approve a wider content budget",
            ]
        if isinstance(page_count, int) and page_count > 1:
            actions = [
                "after compact_2, propose removing only the lowest-ranked whole project when more than three remain",
                "otherwise ask the user which project may be removed",
            ]
        status = "candidate_ready" if candidate_path and not recovery_already_applied else "needs_user_input"
    elif route == "evidence_review":
        actions = [
            "identify the first unauthorized, cross-project, or unsupported assertion",
            "ask the user to approve a source or remove the assertion",
        ]
        status = "evidence_review_pending"
    else:
        actions = ["manual diagnosis required before any mutation"]
        status = "manual_review_pending"
    path = runtime_root / "recovery-requests" / f"{event['event_id']}.json"
    payload = {
        "event_id": event["event_id"],
        "failure_signature": event["failure_signature"],
        "route": route,
        "status": status,
        "error_code": event["error_code"],
        "measurements": event.get("measurements", {}),
        "actions": actions,
        "constraints": [
            "use only authorized private.yaml Claims",
            "never invent facts, metrics, employers, dates, or projects",
            "re-run the full content and PDF/DOCX gates before delivery",
        ],
        "created_at": now_iso(),
    }
    if candidate_path:
        payload["candidate_path"] = str(candidate_path)
        payload["unused_claim_count"] = len(unused_claims)
        if typeset_candidate is not None:
            payload["typeset_plan_candidate_path"] = str(candidate_path.with_name("typeset-plan-candidate.json"))
    atomic_write(path, payload)
    return path


def build_offline_rule_candidate(event: dict[str, Any], runtime_root: Path, skill_path: Path) -> Path | None:
    """Create a deterministic, bounded rule candidate when no API benchmark is configured.

    This is deliberately a *pending* candidate, never an acceptance decision:
    the frozen benchmark/Validation Gate is still required before any human
    merge.  It ensures a public layout failure produces useful SkillOpt work
    instead of remaining at ``queued`` forever in a zero-configuration setup.
    """
    hint = OFFLINE_RULE_HINTS.get(str(event.get("error_code")))
    if hint is None or not skill_path.is_file():
        return None
    alias, instruction = hint
    try:
        from skillopt_pipeline import SECTION_ALIASES, apply_bounded_patch, markdown_to_sections, split_frontmatter

        skill_text = skill_path.read_text(encoding="utf-8")
        _, body = split_frontmatter(skill_text)
        sections, _ = markdown_to_sections(body)
        actual = next((candidate for candidate in SECTION_ALIASES[alias] if candidate in sections), None)
        if not actual:
            return None
        updated = sections[actual].rstrip() + f"\n\n- SkillOpt 候选规则：{instruction}\n"
        operation = {"op": "replace", "path": f"/sections/{alias}", "value": updated}
        candidate_text, _ = apply_bounded_patch(skill_text, [operation])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    candidate_id = f"offline-{event['event_id']}"
    candidate_dir = runtime_root / "candidates" / candidate_id
    candidate_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = candidate_dir / "skill_candidate.md"
    candidate_path.write_text(candidate_text, encoding="utf-8")
    proposal = {
        "summary": f"Offline bounded candidate for {event.get('error_code')}",
        "hypothesis": "The deterministic gate failure needs an explicit post-render measurement rule.",
        "patch": [operation],
        "expected_effect": "Make the observed public delivery check explicit; benchmark validation remains mandatory.",
        "source": "deterministic_offline_fallback",
    }
    atomic_write(candidate_dir / "proposal.json", proposal)
    diagnosis_path = runtime_root / "diagnoses" / f"{event['event_id']}.json"
    if diagnosis_path.is_file():
        shutil.copy2(diagnosis_path, candidate_dir / "diagnosis.json")
    return candidate_path


def run_optimizer(event: dict[str, Any], runtime_root: Path, skill_path: Path,
                  benchmark_command: str, proposal: Path | None, execute: bool,
                  cooldown_seconds: int) -> dict[str, Any]:
    if in_cooldown(runtime_root, event["failure_signature"], cooldown_seconds):
        event["auto_skillopt"]["status"] = "cooldown"
        # Keep the previously generated recovery request discoverable.  A
        # cooldown must suppress duplicate optimizer work, not erase the
        # user's actionable next step from the build result.
        existing_request = runtime_root / "recovery-requests" / f"{event['event_id']}.json"
        if existing_request.is_file():
            event["auto_skillopt"]["recovery_request"] = str(existing_request)
        return event
    if not event["auto_skillopt"]["eligible"]:
        request = write_content_recovery_request(runtime_root, event)
        request_payload = read_json(request)
        recovery_status = request_payload.get("status", "needs_user_input")
        event["auto_skillopt"].update({
            "status": recovery_status,
            "recovery_request": str(request),
            "reason": "entered SkillOpt controller; protected lane cannot mutate active rules",
        })
        write_cooldown(runtime_root, event, cooldown_seconds, event["auto_skillopt"]["status"])
        return event
    if not benchmark_command:
        candidate_path = build_offline_rule_candidate(event, runtime_root, skill_path)
        if candidate_path:
            event["auto_skillopt"].update({
                "status": "candidate_pending_validation",
                "candidate_path": str(candidate_path),
                "reason": "offline bounded candidate created; frozen benchmark and human merge are still required",
            })
            write_cooldown(runtime_root, event, cooldown_seconds, "candidate_pending_validation")
        else:
            event["auto_skillopt"].update({
                "status": "queued",
                "reason": "public-rule candidate requires SKILLOPT_BENCHMARK_COMMAND",
            })
        return event
    if execute == bool(proposal):
        event["auto_skillopt"].update({"status": "queued", "reason": "provide exactly one of --proposal or --execute"})
        return event
    trajectory_dir = materialize_trajectory(event, runtime_root).parent
    command = [sys.executable, str(Path(__file__).with_name("skillopt_pipeline.py")),
               "--skill-path", str(skill_path), "--failure-dir", str(trajectory_dir),
               "--runtime-root", str(runtime_root), "--benchmark-command", benchmark_command]
    if proposal:
        command.extend(["--proposal", str(proposal)])
    else:
        command.append("--execute")
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    event["auto_skillopt"].update({
        "status": "candidate_evaluated" if completed.returncode in {0, 2} else "optimizer_error",
        "returncode": completed.returncode,
    })
    if completed.stdout.strip():
        event["auto_skillopt"]["optimizer_output_tail"] = completed.stdout[-2000:]
        # SkillOpt prints its final event as JSON.  Copy the audit inputs next
        # to the candidate so a review can be reproduced without consulting
        # mutable environment variables.
        try:
            output_payload = json.loads(completed.stdout)
            if output_payload.get("decision") == "accepted":
                event["auto_skillopt"]["status"] = "candidate_accepted"
            elif output_payload.get("decision") == "rejected":
                event["auto_skillopt"]["status"] = "candidate_rejected"
            if output_payload.get("decision"):
                event["auto_skillopt"]["decision"] = output_payload["decision"]
            candidate_path = output_payload.get("candidate_path")
            if isinstance(candidate_path, str) and Path(candidate_path).is_file():
                candidate_dir = Path(candidate_path).parent
                if proposal and proposal.is_file():
                    shutil.copy2(proposal, candidate_dir / "proposal.json")
                diagnosis = runtime_root / "diagnoses" / f"{event['event_id']}.json"
                if diagnosis.is_file():
                    shutil.copy2(diagnosis, candidate_dir / "diagnosis.json")
                event["auto_skillopt"]["candidate_path"] = candidate_path
        except (json.JSONDecodeError, OSError):
            # The pipeline's textual output remains in the event tail; an
            # audit-copy failure must not change the benchmark decision.
            pass
    if completed.returncode not in {0, 2}:
        event["auto_skillopt"]["stderr_tail"] = completed.stderr[-2000:]
    write_cooldown(runtime_root, event, cooldown_seconds, event["auto_skillopt"]["status"])
    return event


def rollback(runtime_root: Path, event_id: str, reason: str) -> Path:
    pointer = runtime_root / "active_candidate.json"
    previous = read_json(pointer) if pointer.is_file() else {"active_candidate": None, "incumbent_sha256": None}
    active_path = Path(str(previous.get("active_path"))) if previous.get("active_path") else None
    incumbent_path = Path(str(previous.get("incumbent_path"))) if previous.get("incumbent_path") else None
    restored_path = None
    if active_path and incumbent_path and active_path.is_file() and incumbent_path.is_file():
        temporary = active_path.with_name(f".{active_path.name}.{uuid.uuid4().hex}.rollback")
        shutil.copy2(incumbent_path, temporary)
        os.replace(temporary, active_path)
        restored_path = str(active_path)
    pointer.unlink(missing_ok=True)
    path = runtime_root / "rollback" / f"{event_id}.json"
    atomic_write(path, {"status": "rolled_back", "event_id": event_id, "reason": reason,
                        "restored_incumbent": previous.get("incumbent_sha256"),
                        "restored_path": restored_path, "created_at": now_iso()})
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    diag = sub.add_parser("diagnose")
    diag.add_argument("--failed-manifest", type=Path, required=True)
    diag.add_argument("--runtime-root", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--failed-manifest", type=Path, required=True)
    run.add_argument("--runtime-root", type=Path, required=True)
    run.add_argument("--skill-path", type=Path, required=True)
    run.add_argument("--benchmark-command", default="")
    run.add_argument("--proposal", type=Path)
    run.add_argument("--execute", action="store_true")
    run.add_argument("--cooldown-seconds", type=int, default=DEFAULT_COOLDOWN_SECONDS)
    rb = sub.add_parser("rollback")
    rb.add_argument("--runtime-root", type=Path, required=True)
    rb.add_argument("--event-id", required=True)
    rb.add_argument("--reason", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "diagnose":
            event_path, event = diagnose(args.failed_manifest.resolve(), args.runtime_root.resolve())
            print(json.dumps({"status": event["auto_skillopt"]["status"], "event": str(event_path),
                              "event_id": event["event_id"], "eligible": event["auto_skillopt"]["eligible"]}, ensure_ascii=False))
            return 0
        if args.command == "rollback":
            path = rollback(args.runtime_root.resolve(), args.event_id, args.reason)
            print(json.dumps({"status": "rolled_back", "rollback": str(path)}, ensure_ascii=False))
            return 0
        event_path, event = diagnose(args.failed_manifest.resolve(), args.runtime_root.resolve())
        event = run_optimizer(event, args.runtime_root.resolve(), args.skill_path.resolve(),
                              args.benchmark_command, args.proposal.resolve() if args.proposal else None,
                              args.execute, args.cooldown_seconds)
        atomic_write(event_path, event)
        print(json.dumps({"status": event["auto_skillopt"]["status"], "event": str(event_path),
                          "event_id": event["event_id"]}, ensure_ascii=False))
        return 0 if event["auto_skillopt"]["status"] in {
            "candidate_evaluated", "candidate_accepted", "candidate_rejected",
            "candidate_ready", "candidate_pending_validation", "needs_user_input", "diagnosed", "manual_review_pending",
            "cooldown", "queued",
        } else 2
    except AutoLoopError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
