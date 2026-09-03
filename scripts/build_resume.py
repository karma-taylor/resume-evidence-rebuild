#!/usr/bin/env python3
"""Evidence-first resume pipeline: profile + template -> plans -> Typst PDF."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator, model_validator
from design_tokens import DESIGN_VARIANTS, theme_payload, theme_review_payload
from validate_resume_artifacts import (
    ResumeQAError,
    atomic_write_json,
    begin_render_transaction,
    check_delivery_manifest,
    promote_render_transaction,
    quarantine_artifacts,
    quarantine_render_transaction,
    run_docx_delivery_gate,
    write_delivery_manifest,
    MAX_BOTTOM_WHITESPACE_PT,
)


CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
NUMERIC_TOKEN_RE = re.compile(r"[0-9０-９]+(?:[.,．][0-9０-９]+)?[%％]?")
VALUE_FRAGMENT_RE = re.compile(r"([0-9０-９]+(?:[.,．][0-9０-９]+)?)\s*(秒|分钟|小时|天|周|月|年|个|项|单|%|％)?")
UNIT_TO_BASE = {
    "秒": 1 / 60, "分钟": 1, "小时": 60, "天": 1440,
    "周": 10080, "月": 43200, "年": 525600,
}
METRIC_EFFECT_TERMS = ("提升", "降低")
ARCHITECTURE_TERMS = ("架构", "重构", "RAG", "检索", "召回", "Reranker", "ACL")
OWNERSHIP_TERMS = ("独立", "0到1", "首个")
TERMINAL_TRAILING_RE = re.compile(r"^[\s。．.！!？?；;，,、」』》。]*$")
UNBOUND_PUNCTUATION = frozenset("，。．、；;：:！!？?（）()（）【】[]{}「」『』《》〈〉—–-－·/\\|+&")
# Technical vocabulary is intentionally small and frozen.  It is used only
# for readability placement/density checks; it never decides whether a Claim
# is true.  Product names and concrete business nouns stay in the evidence
# layer and are not guessed here.
TECHNICAL_TERM_RE = re.compile(
    r"(?i)(?:Python|FastAPI|Django|Flask|Pydantic|LangChain|LangGraph|LlamaIndex|OpenClaw|RAG|Agent|API|Cron|ACL|JWT|JSON|BM25|Reranker|StrictBool|StrictInt|Decimal|Schema|Patch|Worker|Turnstile|MCP|LoRA|向量数据库|向量|模型|算法|架构|重构|检索|召回|鉴权|序列化|异步|接口|编排|调度)"
)
BUSINESS_CONTEXT_KINDS = frozenset({"context"})
BUSINESS_ACTION_KINDS = frozenset({"architecture", "control", "delivery"})
BUSINESS_RESULT_KINDS = frozenset({"metric", "delivery", "control"})
MAX_TECHNICAL_TERMS_PER_BULLET = 2
# The first render uses the compact 40–50 contract.  Content recovery may
# select one of two explicitly bounded budgets; it never accepts arbitrary
# model-provided limits.
CONTENT_BOUNDS: dict[str, tuple[int, int]] = {
    "normal": (40, 50),
    "compressed": (30, 40),
    # Sparse-page recovery may use more Claim-backed text than the normal
    # 40–50 budget.  The upper bound is still finite so recovery cannot turn
    # into unbounded prose generation or a hidden layout change.
    "expanded": (50, 130),
}


def has_metric_assertion(text: str) -> bool:
    """Numbers and unquantified improvement claims both require metric proof."""
    return bool(NUMERIC_TOKEN_RE.search(text) or any(term in text for term in METRIC_EFFECT_TERMS))


def result_kinds_for(text: str) -> set[str]:
    """Return the only permissible source kinds for a terminal result."""
    return {"metric"} if has_metric_assertion(text) else {"architecture", "control", "delivery"}


def technical_terms(text: str) -> list[str]:
    """Return distinct technical terms in display order.

    This is a readability signal, not an evidence shortcut.  The caller must
    still prove every matched span through the normal assertion/Claim gates.
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in TECHNICAL_TERM_RE.finditer(text):
        normalized = match.group(0).lower()
        if normalized not in seen:
            seen.add(normalized)
            result.append(match.group(0))
    return result


def technical_term_positions(text: str) -> list[tuple[str, int, int]]:
    return [(match.group(0), match.start(), match.end()) for match in TECHNICAL_TERM_RE.finditer(text)]


def raise_business_error(code: str, detail: str) -> None:
    raise EvidenceGateError(f"{code}: {detail}")


def validate_project_business_readability(text: str, stage: str, label: str) -> None:
    """Keep project prose business-first without weakening evidence checks."""
    terms = technical_terms(text)
    if stage in {"background", "result"} and terms:
        raise_business_error(
            "TECHNICAL_TERM_PLACEMENT_ERROR",
            f"{label} may not place technical terms {terms!r} outside the solution bullet",
        )
    if stage == "solution" and len(terms) > MAX_TECHNICAL_TERMS_PER_BULLET:
        raise_business_error(
            "TECHNICAL_TERM_OVERLOAD",
            f"{label} contains {len(terms)} technical terms; maximum is {MAX_TECHNICAL_TERMS_PER_BULLET}",
        )


def validate_work_business_readability(
    text: str,
    assertions: list[EmploymentAssertion],
    source_supports: Any,
    declared_sources: set[str],
    label: str,
) -> None:
    """Require a business context plus an authorized action in work bullets.

    Direct work bullets intentionally have no visible stage labels.  Their
    assertion source kinds provide the auditable structure instead.
    """
    context_spans: list[tuple[int, int]] = []
    action_spans: list[tuple[int, int]] = []
    result_spans: list[tuple[int, int]] = []
    for assertion in assertions:
        if assertion.source_ingestion_id not in declared_sources:
            continue
        start = text.find(assertion.text)
        if start < 0:
            continue
        span = (start, start + len(assertion.text))
        if source_supports(assertion.source_ingestion_id, assertion.text, "context"):
            context_spans.append(span)
        if any(source_supports(assertion.source_ingestion_id, assertion.text, kind) for kind in BUSINESS_ACTION_KINDS):
            action_spans.append(span)
        if any(source_supports(assertion.source_ingestion_id, assertion.text, kind) for kind in BUSINESS_RESULT_KINDS):
            result_spans.append(span)
    if not context_spans:
        raise_business_error("BUSINESS_CONTEXT_MISSING", f"{label} lacks an authorized business-context Claim")
    if not action_spans:
        raise_business_error("BUSINESS_ACTION_MISSING", f"{label} lacks an authorized business-action Claim")
    if not result_spans:
        raise_business_error("BUSINESS_RESULT_MISSING", f"{label} lacks an authorized business-result or delivery Claim")

    terms = technical_term_positions(text)
    if len({term.lower() for term, _, _ in terms}) > MAX_TECHNICAL_TERMS_PER_BULLET:
        raise_business_error(
            "TECHNICAL_TERM_OVERLOAD",
            f"{label} contains more than {MAX_TECHNICAL_TERMS_PER_BULLET} technical terms",
        )
    for term, start, end in terms:
        if not any(span_start <= start and end <= span_end for span_start, span_end in action_spans):
            raise_business_error(
                "TECHNICAL_TERM_PLACEMENT_ERROR",
                f"{label} technical term {term!r} must stay inside the business action fragment",
            )


def fragment_numeric_value(fragment: str) -> float | None:
    """Extract one explicit number and normalize common human units.

    This prevents a model from pairing a real ``2小时 → 10分钟`` Claim with
    arbitrary declared inputs such as ``999 → 1``.  Unknown/count units stay in
    their written unit and therefore still require matching values.
    """
    normalized = unicodedata.normalize("NFKC", fragment)
    match = VALUE_FRAGMENT_RE.search(normalized)
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    unit = match.group(2)
    return number * UNIT_TO_BASE.get(unit, 1.0)


def declared_value_matches_fragment(value: float, fragment: str) -> bool:
    parsed = fragment_numeric_value(fragment)
    return parsed is not None and math.isclose(parsed, value, rel_tol=1e-6, abs_tol=1e-6)


class RetryableContractError(ValueError):
    """Agent B may rewrite the same Claim set for this contract error only."""


class EvidenceGateError(ValueError):
    """Evidence, authorization, or project-boundary failure; never retry."""


class NeedsUserInputError(ValueError):
    """The facts are valid but insufficient to safely fulfil the request."""


class Claim(BaseModel):
    id: str
    text: str
    source: str
    scope: str
    confidence: Literal["verified", "bounded"]
    allowed_for_resume: bool
    kind: Literal["context", "architecture", "control", "metric", "delivery"]


class Project(BaseModel):
    id: str
    title: str
    start: str
    end: str
    tags: list[str] = Field(default_factory=list)
    claims: list[Claim]


class Identity(BaseModel):
    name: str
    phone: str
    email: str
    portfolio_url: HttpUrl
    market: Literal["CN", "NA", "FOREIGN"]
    photo_path: str | None = None
    location: str | None = None


class WorkHighlight(BaseModel):
    text: str = Field(min_length=1)
    source_ingestion_id: str = Field(min_length=1)
    approved_at: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class Employment(BaseModel):
    employer: str
    title: str
    start: str
    end: str
    summary: str = ""
    highlights: list[WorkHighlight] = Field(default_factory=list, max_length=5)


class Profile(BaseModel):
    identity: Identity
    education: list[dict]
    employment: list[Employment]
    certifications: list[str] = Field(default_factory=list)
    projects: list[Project]


class Template(BaseModel):
    id: str
    target_role: str
    market: Literal["CN", "NA", "FOREIGN"]
    # A legacy template may pin a role to known-good projects.  JD-driven
    # selection is opt-in and replaces this list only after its evidence map
    # has been verified (see ``resolve_project_selection`` below).
    project_ids: list[str] = Field(default_factory=list, max_length=4)
    layout: dict
    technical_skills: str = ""
    summary: str = ""
    summary_bold_phrases: list[str] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=lambda: [
        "profile", "technical-skills", "employment", "projects", "education-certifications",
    ])

    @model_validator(mode="after")
    def enforce_section_order(self) -> "Template":
        expected = ["profile", "technical-skills", "employment", "projects", "education-certifications"]
        if self.sections != expected:
            raise ValueError("template sections must be profile → technical-skills → employment → projects → education-certifications")
        return self


class ProbeProject(BaseModel):
    id: str
    status: Literal["ready", "bounded", "needs_user_input", "evidence_gate_blocked"]
    questions: list[str] = Field(default_factory=list)


class ResumePlanProject(BaseModel):
    id: str
    title: str
    start: str
    end: str
    tags: list[str]
    claim_ids: list[str]
    claims: list[Claim]


class ResumePlan(BaseModel):
    target_role: str
    projects: list[ResumePlanProject] = Field(min_length=3, max_length=4)
    selection: "ProjectSelection | None" = None


class ProjectSelection(BaseModel):
    """Auditable project scope used for one resume build.

    ``jd`` never adds content facts.  It only records why already-authorized
    projects were selected; Agent B remains restricted to the selected
    project's private Claims.
    """

    mode: Literal["template", "jd"]
    project_ids: list[str] = Field(min_length=3, max_length=4)
    jd_text_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    evidence_map_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


class JDRequirement(BaseModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)
    priority: Literal["required", "preferred"] = "required"


class JDBrief(BaseModel):
    schema_version: Literal["1.0"]
    target_role: str = Field(min_length=1)
    jd_text_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    requirements: list[JDRequirement] = Field(min_length=1)
    # Three projects are the single-page default.  A fourth needs an
    # intentional caller choice and still remains subject to the normal
    # no-delete/no-shrink reflow gate.
    max_projects: Literal[3, 4] = 3


class JDEvidenceMatch(BaseModel):
    project_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    excerpt: str = Field(min_length=1)
    matched_keywords: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_line_range(self) -> "JDEvidenceMatch":
        if self.line_end < self.line_start:
            raise ValueError("JD evidence line_end must not precede line_start")
        return self


class JDEvidenceMap(BaseModel):
    schema_version: Literal["1.0"]
    jd_text_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    matches: list[JDEvidenceMatch] = Field(min_length=1)


class AssertionFragment(BaseModel):
    text: str = Field(min_length=1)
    source_claim_id: str = Field(min_length=1)


class DerivedMetric(BaseModel):
    """A deterministic calculation whose inputs are verbatim Claim text."""

    operation: Literal["percentage_reduction", "percentage_increase", "times_improvement"]
    source_claim_ids: list[str] = Field(min_length=1, max_length=3)
    before_text: str = Field(min_length=1)
    after_text: str = Field(min_length=1)
    # Values use one explicit common unit (for example 120 and 10 minutes).
    # The source fragments above preserve the human-facing original units.
    before_value: float = Field(gt=0)
    after_value: float = Field(ge=0)
    precision: int = Field(default=1, ge=0, le=2)


class BusinessSegment(BaseModel):
    """One evidence-addressable part of a business-first bullet."""

    text: str = Field(min_length=1)
    source_claim_id: str = Field(min_length=1)
    derived_metric: DerivedMetric | None = None


class BusinessBulletStructure(BaseModel):
    """Business-first copy contract for readers who are not domain experts."""

    business_difficulty: BusinessSegment
    solution_action: BusinessSegment
    # Kept as ``quantified_result`` for compatibility with existing Agent B
    # JSON.  Its value is a verified result: numeric when a metric exists,
    # otherwise an authorized delivery/control/architecture conclusion.
    quantified_result: BusinessSegment

    @model_validator(mode="after")
    def derived_metric_only_on_result(self) -> "BusinessBulletStructure":
        if self.business_difficulty.derived_metric or self.solution_action.derived_metric:
            raise ValueError("derived_metric is only permitted on quantified_result")
        return self


class EmploymentAssertion(BaseModel):
    """A verbatim source fragment used to compose an approved work bullet."""

    text: str = Field(min_length=1)
    source_ingestion_id: str = Field(min_length=1)


class EmploymentBusinessSegment(BaseModel):
    """One business-first work-bullet segment with an approved inbox source."""

    text: str = Field(min_length=1)
    source_ingestion_id: str = Field(min_length=1)
    derived_metric: DerivedMetric | None = None


class EmploymentBusinessBulletStructure(BaseModel):
    business_difficulty: EmploymentBusinessSegment
    solution_action: EmploymentBusinessSegment
    quantified_result: EmploymentBusinessSegment

    @model_validator(mode="after")
    def derived_metric_only_on_result(self) -> "EmploymentBusinessBulletStructure":
        if self.business_difficulty.derived_metric or self.solution_action.derived_metric:
            raise ValueError("derived_metric is only permitted on quantified_result")
        return self


def validate_business_bullet_contract(
    *, text: str, bold_phrases: list[str], terminal_phrase: str,
    structure: BusinessBulletStructure | EmploymentBusinessBulletStructure,
) -> None:
    """Apply the broad safety envelope before rendering.

    The exact 40–50/30–40/50–130 budget is selected by ``TypesetPlan`` and
    enforced in ``validate_agent_b``.  Keeping model construction within this
    30–130 envelope lets a bounded recovery candidate be validated without
    weakening the normal first-render contract.
    """
    count = len(CJK_RE.findall(text))
    if not 30 <= count <= 130:
        raise RetryableContractError(f"BULLET_LENGTH_ERROR: expected 30-130 CJK characters; got {count}")
    for phrase in bold_phrases:
        if phrase not in text:
            raise RetryableContractError(f"BULLET_BOLD_MISSING_ERROR: {phrase!r} is absent from bullet text")
    if terminal_phrase not in bold_phrases:
        raise RetryableContractError("TERMINAL_BOLD_ERROR: terminal_bold_phrase must be declared in bold_phrases_used")
    index = text.rfind(terminal_phrase)
    if index < 0 or not TERMINAL_TRAILING_RE.fullmatch(text[index + len(terminal_phrase):]):
        raise RetryableContractError("TERMINAL_BOLD_ERROR: terminal_bold_phrase must end the final semantic clause")
    ordered = (
        structure.business_difficulty.text,
        structure.solution_action.text,
        structure.quantified_result.text,
    )
    positions = [text.find(part) for part in ordered]
    if (any(position < 0 for position in positions)
            or len(set(ordered)) != 3
            or not (positions[0] + len(ordered[0]) <= positions[1]
                    and positions[1] + len(ordered[1]) <= positions[2])):
        raise RetryableContractError(
            "BUSINESS_BULLET_STRUCTURE_ERROR: business segments must be distinct, non-overlapping, and ordered business difficulty → action → result"
        )
    if structure.quantified_result.text != terminal_phrase:
        raise RetryableContractError(
            "BUSINESS_BULLET_STRUCTURE_ERROR: quantified_result must be the terminal_bold_phrase"
        )
    metric_assertion = has_metric_assertion(text)
    if metric_assertion and not NUMERIC_TOKEN_RE.search(structure.quantified_result.text):
        raise RetryableContractError(
            "BUSINESS_BULLET_STRUCTURE_ERROR: a numeric or improvement bullet must end with its numeric result"
        )


def derived_metric_value(metric: DerivedMetric) -> float:
    if metric.operation == "percentage_reduction":
        if metric.after_value >= metric.before_value:
            raise EvidenceGateError("DERIVED_METRIC_ERROR: percentage reduction requires after < before")
        return (metric.before_value - metric.after_value) / metric.before_value * 100
    if metric.operation == "percentage_increase":
        if metric.after_value <= metric.before_value:
            raise EvidenceGateError("DERIVED_METRIC_ERROR: percentage increase requires after > before")
        return (metric.after_value - metric.before_value) / metric.before_value * 100
    if metric.after_value <= 0 or metric.before_value <= metric.after_value:
        raise EvidenceGateError("DERIVED_METRIC_ERROR: times improvement requires before > after > 0")
    return metric.before_value / metric.after_value


def validate_derived_metric(
    *, metric: DerivedMetric, result_text: str, claims: dict[str, Claim],
    declared_claim_ids: list[str], label: str,
) -> None:
    """Verify formula, inputs, units and display before accepting a derived result."""
    if not set(metric.source_claim_ids).issubset(set(declared_claim_ids)):
        raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: {label} derived metric references an undeclared Claim")
    source_claims = [claims.get(claim_id) for claim_id in metric.source_claim_ids]
    if any(claim is None or claim.kind != "metric" or not claim.allowed_for_resume for claim in source_claims):
        raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: {label} derived metric requires allowed metric Claim sources")
    if not any(
        metric.before_text in claim.text and metric.after_text in claim.text
        for claim in source_claims if claim is not None
    ):
        raise EvidenceGateError(
            f"EVIDENCE_GATE_BLOCKED: {label} derived metric inputs must be verbatim in one metric Claim"
        )
    if not declared_value_matches_fragment(metric.before_value, metric.before_text):
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} before_value does not match before_text")
    if not declared_value_matches_fragment(metric.after_value, metric.after_text):
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} after_value does not match after_text")
    if metric.before_text == metric.after_text:
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} before_text and after_text must differ")
    value = derived_metric_value(metric)
    expected = f"{value:.{metric.precision}f}"
    normalized_result = unicodedata.normalize("NFKC", result_text)
    if expected not in normalized_result:
        raise EvidenceGateError(
            f"DERIVED_METRIC_ERROR: {label} result must display computed value {expected}"
        )
    if metric.operation.startswith("percentage_") and not re.search(r"[%％]", normalized_result):
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} percentage result must show % or ％")
    if metric.operation == "times_improvement" and "倍" not in normalized_result:
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} times result must show 倍")


def validate_derived_employment_metric(
    *, metric: DerivedMetric, result_text: str, declared_source_ids: set[str],
    source_supports: Any, label: str,
) -> None:
    """Apply the same deterministic formula gate to approved inbox facts.

    Employment evidence is keyed by ``source_ingestion_id`` rather than a
    project Claim id, so it uses the inbox resolver while preserving the same
    one-source, verbatim before/after requirement.
    """
    metric_sources = set(metric.source_claim_ids)
    if not metric_sources.issubset(declared_source_ids):
        raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: {label} derived metric references an undeclared ingestion source")
    if not any(
        source_supports(source_id, metric.before_text, "metric")
        and source_supports(source_id, metric.after_text, "metric")
        for source_id in metric_sources
    ):
        raise EvidenceGateError(
            f"EVIDENCE_GATE_BLOCKED: {label} derived metric inputs must be verbatim in one metric inbox source"
        )
    if not declared_value_matches_fragment(metric.before_value, metric.before_text):
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} before_value does not match before_text")
    if not declared_value_matches_fragment(metric.after_value, metric.after_text):
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} after_value does not match after_text")
    if metric.before_text == metric.after_text:
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} before_text and after_text must differ")
    value = derived_metric_value(metric)
    expected = f"{value:.{metric.precision}f}"
    normalized_result = unicodedata.normalize("NFKC", result_text)
    if expected not in normalized_result:
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} result must display computed value {expected}")
    if metric.operation.startswith("percentage_") and not re.search(r"[%％]", normalized_result):
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} percentage result must show % or ％")
    if metric.operation == "times_improvement" and "倍" not in normalized_result:
        raise EvidenceGateError(f"DERIVED_METRIC_ERROR: {label} times result must show 倍")


class Overview(BaseModel):
    text: str = Field(min_length=1)
    source_claim_ids: list[str] = Field(min_length=1)
    assertions: list[AssertionFragment] = Field(min_length=1)


class TypesetBullet(BaseModel):
    text: str
    bold_phrases_used: list[str] = Field(min_length=1, max_length=2)
    source_claim_ids: list[str] = Field(min_length=1)
    terminal_bold_phrase: str = Field(min_length=1)
    assertions: list[AssertionFragment] = Field(min_length=1)
    business_structure: BusinessBulletStructure

    @field_validator("bold_phrases_used")
    @classmethod
    def no_empty_phrases(cls, value: list[str]) -> list[str]:
        if not all(item.strip() for item in value):
            raise ValueError("bold_phrases_used cannot contain empty strings")
        return value

    @model_validator(mode="after")
    def validate_static_contract(self) -> "TypesetBullet":
        validate_business_bullet_contract(
            text=self.text,
            bold_phrases=self.bold_phrases_used,
            terminal_phrase=self.terminal_bold_phrase,
            structure=self.business_structure,
        )
        return self


class ProjectStageBullet(BaseModel):
    """One of the three project-level bullets: background, solution, result."""

    stage: Literal["background", "solution", "result"]
    text: str
    bold_phrases_used: list[str] = Field(min_length=1, max_length=2)
    terminal_bold_phrase: str | None = None
    source_claim_ids: list[str] = Field(min_length=1)
    assertions: list[AssertionFragment] = Field(min_length=1)
    derived_metric: DerivedMetric | None = None

    @model_validator(mode="after")
    def validate_stage_contract(self) -> "ProjectStageBullet":
        count = len(CJK_RE.findall(self.text))
        if not 30 <= count <= 130:
            raise RetryableContractError(f"BULLET_LENGTH_ERROR: project {self.stage} bullet expected 30-130 CJK characters; got {count}")
        if any(phrase not in self.text for phrase in self.bold_phrases_used):
            raise RetryableContractError("BULLET_BOLD_MISSING_ERROR: stage bold phrase is absent from bullet text")
        if self.stage == "result":
            if not self.terminal_bold_phrase or self.terminal_bold_phrase not in self.bold_phrases_used:
                raise RetryableContractError("TERMINAL_BOLD_ERROR: result bullet requires terminal_bold_phrase")
            index = self.text.rfind(self.terminal_bold_phrase)
            if index < 0 or not TERMINAL_TRAILING_RE.fullmatch(self.text[index + len(self.terminal_bold_phrase):]):
                raise RetryableContractError("TERMINAL_BOLD_ERROR: result terminal phrase must end the final semantic clause")
            if not has_metric_assertion(self.text):
                raise NeedsUserInputError("NEEDS_USER_INPUT: project result bullet requires an authorized numeric or derived efficiency metric")
            if self.derived_metric is None and not NUMERIC_TOKEN_RE.search(self.terminal_bold_phrase):
                raise RetryableContractError("TERMINAL_BOLD_ERROR: result terminal phrase must contain a numeric metric")
        elif self.terminal_bold_phrase is not None:
            raise RetryableContractError("TERMINAL_BOLD_ERROR: only result bullet may declare terminal_bold_phrase")
        return self


class TypesetEmploymentBullet(BaseModel):
    """A permitted re-composition of user-confirmed employment source facts.

    Work history is intentionally *not* forced into the project-level
    background/solution/result structure.  ``business_structure`` remains an
    optional backwards-compatible field for older Agent B payloads, but new
    payloads may provide a direct business-oriented sentence instead.
    """

    text: str
    bold_phrases_used: list[str] = Field(min_length=1, max_length=2)
    terminal_bold_phrase: str = Field(min_length=1)
    source_ingestion_ids: list[str] = Field(min_length=1)
    assertions: list[EmploymentAssertion] = Field(min_length=1)
    business_structure: EmploymentBusinessBulletStructure | None = None

    @field_validator("bold_phrases_used")
    @classmethod
    def no_empty_phrases(cls, value: list[str]) -> list[str]:
        if not all(item.strip() for item in value):
            raise ValueError("bold_phrases_used cannot contain empty strings")
        return value

    @model_validator(mode="after")
    def validate_static_contract(self) -> "TypesetEmploymentBullet":
        if self.business_structure is not None:
            validate_business_bullet_contract(
                text=self.text,
                bold_phrases=self.bold_phrases_used,
                terminal_phrase=self.terminal_bold_phrase,
                structure=self.business_structure,
            )
        else:
            # Direct work bullets keep the evidence and terminal emphasis
            # contract, while deliberately omitting project-only stage labels.
            count = len(CJK_RE.findall(self.text))
            if not 30 <= count <= 130:
                raise RetryableContractError(
                    f"BULLET_LENGTH_ERROR: employment bullet expected 30-130 CJK characters; got {count}"
                )
            if any(phrase not in self.text for phrase in self.bold_phrases_used):
                raise RetryableContractError("BULLET_BOLD_MISSING_ERROR: employment bold phrase is absent from bullet text")
            if self.terminal_bold_phrase not in self.bold_phrases_used:
                raise RetryableContractError("TERMINAL_BOLD_ERROR: employment terminal phrase must be declared in bold_phrases_used")
            index = self.text.rfind(self.terminal_bold_phrase)
            if index < 0 or not TERMINAL_TRAILING_RE.fullmatch(self.text[index + len(self.terminal_bold_phrase):]):
                raise RetryableContractError("TERMINAL_BOLD_ERROR: employment terminal phrase must end the final semantic clause")
        return self


class TypesetProject(BaseModel):
    id: str
    overview: Overview | None = None
    bullets: list[TypesetBullet | ProjectStageBullet] = Field(min_length=3, max_length=4)

    @model_validator(mode="after")
    def validate_project_stage_set(self) -> "TypesetProject":
        staged = [bullet for bullet in self.bullets if isinstance(bullet, ProjectStageBullet)]
        if staged:
            if len(staged) != 3 or len(staged) != len(self.bullets) or {bullet.stage for bullet in staged} != {"background", "solution", "result"}:
                raise RetryableContractError("BUSINESS_BULLET_STRUCTURE_ERROR: each project must contain exactly background, solution, and result bullets")
            if self.overview is not None:
                raise RetryableContractError("BUSINESS_BULLET_STRUCTURE_ERROR: staged projects must not duplicate background in overview")
        elif not 3 <= len(self.bullets) <= 4:
            raise RetryableContractError("BULLET_LENGTH_ERROR: legacy project requires 3-4 bullets")
        return self


class TypesetEmployment(BaseModel):
    id: str = Field(min_length=1)
    bullets: list[TypesetEmploymentBullet] = Field(min_length=4, max_length=5)


class TypesetPlan(BaseModel):
    projects: list[TypesetProject] = Field(min_length=3, max_length=4)
    employment: list[TypesetEmployment]
    content_mode: Literal["normal", "compressed", "expanded"] = "normal"


def cjk_count(text: str) -> int:
    return len(CJK_RE.findall(text))


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def selection_for_template(template: Template) -> ProjectSelection:
    if not 3 <= len(template.project_ids) <= 4:
        raise EvidenceGateError(
            "EVIDENCE_GATE_BLOCKED: template mode requires 3-4 explicit project_ids; "
            "provide both --jd-brief and --jd-evidence-map for JD-driven selection"
        )
    if len(template.project_ids) != len(set(template.project_ids)):
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: template project_ids must be unique")
    return ProjectSelection(mode="template", project_ids=template.project_ids)


def _verified_jd_matches(
    *, profile: Profile, brief: JDBrief, evidence_map: JDEvidenceMap,
) -> list[JDEvidenceMatch]:
    """Return only local, current, requirements-backed relevance evidence.

    An evidence map is deliberately not a source of resume facts.  It can
    rank projects, but every rendered sentence must still come from an
    ``allowed_for_resume`` Claim in ``private.yaml``.
    """
    if evidence_map.jd_text_sha256 != brief.jd_text_sha256:
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: JD brief and evidence-map hashes differ")
    projects = {project.id for project in profile.projects}
    requirements = {requirement.id: requirement for requirement in brief.requirements}
    verified: list[JDEvidenceMatch] = []
    for match in evidence_map.matches:
        if match.project_id not in projects:
            raise EvidenceGateError(
                f"EVIDENCE_GATE_BLOCKED: JD evidence references unknown project {match.project_id!r}"
            )
        requirement = requirements.get(match.requirement_id)
        if requirement is None:
            raise EvidenceGateError(
                f"EVIDENCE_GATE_BLOCKED: JD evidence references unknown requirement {match.requirement_id!r}"
            )
        source = Path(match.path)
        if not source.is_file() or sha256(source) != match.source_sha256:
            raise EvidenceGateError(
                f"EVIDENCE_GATE_BLOCKED: JD evidence source changed or is unavailable: {match.path}"
            )
        source_lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        excerpt = "\n".join(source_lines[match.line_start - 1:match.line_end])
        if match.excerpt not in excerpt:
            raise EvidenceGateError(
                f"EVIDENCE_GATE_BLOCKED: JD evidence excerpt no longer matches {match.path}"
            )
        allowed_keywords = set(requirement.keywords)
        if (not set(match.matched_keywords).issubset(allowed_keywords)
                or not all(keyword in match.excerpt for keyword in match.matched_keywords)):
            raise EvidenceGateError(
                f"EVIDENCE_GATE_BLOCKED: JD evidence keyword mapping is invalid for {match.requirement_id}"
            )
        verified.append(match)
    return verified


def resolve_project_selection(
    *, profile: Profile, template: Template, jd_brief_path: Path | None = None,
    jd_evidence_map_path: Path | None = None,
) -> ProjectSelection:
    """Resolve a deterministic, evidence-only project set for this build."""
    if (jd_brief_path is None) != (jd_evidence_map_path is None):
        raise EvidenceGateError(
            "EVIDENCE_GATE_BLOCKED: --jd-brief and --jd-evidence-map must be supplied together"
        )
    if jd_brief_path is None:
        return selection_for_template(template)

    assert jd_evidence_map_path is not None
    brief = JDBrief.model_validate(load_json(jd_brief_path))
    evidence_map = JDEvidenceMap.model_validate(load_json(jd_evidence_map_path))
    if brief.target_role != template.target_role:
        raise EvidenceGateError(
            "EVIDENCE_GATE_BLOCKED: JD brief target_role must exactly match the selected template"
        )
    matches = _verified_jd_matches(profile=profile, brief=brief, evidence_map=evidence_map)
    requirements = {item.id: item for item in brief.requirements}
    per_project: dict[str, dict[str, set[str]]] = {}
    for match in matches:
        buckets = per_project.setdefault(match.project_id, {"required": set(), "preferred": set()})
        buckets[requirements[match.requirement_id].priority].add(match.requirement_id)
    missing_required = sorted(
        requirement.id for requirement in brief.requirements
        if requirement.priority == "required"
        and not any(match.requirement_id == requirement.id for match in matches)
    )
    if missing_required:
        raise NeedsUserInputError(
            "NEEDS_USER_INPUT: local project sources do not support required JD requirements: "
            f"{missing_required}"
        )
    # Rank only by source-backed JD coverage.  Stable project-id ordering
    # makes a tied result auditable rather than model-dependent.
    ranked = sorted(
        per_project,
        key=lambda project_id: (
            -len(per_project[project_id]["required"]),
            -len(per_project[project_id]["preferred"]),
            project_id,
        ),
    )
    if len(ranked) < 3:
        raise NeedsUserInputError(
            "NEEDS_USER_INPUT: fewer than three projects have current, source-backed JD matches"
        )
    return ProjectSelection(
        mode="jd",
        project_ids=ranked[:brief.max_projects],
        jd_text_sha256=brief.jd_text_sha256,
        evidence_map_sha256=sha256(jd_evidence_map_path),
    )


def data_probe(
    profile: Profile, template: Template, selection: ProjectSelection | None = None,
) -> list[ProbeProject]:
    selection = selection or selection_for_template(template)
    by_id = {project.id: project for project in profile.projects}
    results: list[ProbeProject] = []
    for project_id in selection.project_ids:
        project = by_id.get(project_id)
        if not project:
            results.append(ProbeProject(id=project_id, status="needs_user_input", questions=[f"Project {project_id} is absent from private profile."]))
            continue
        questions: list[str] = []
        allowed = [claim for claim in project.claims if claim.allowed_for_resume]
        if not project.start or not project.end:
            questions.append("Provide authorized project start and end dates.")
        if len(allowed) < 3:
            questions.append("Provide at least three authorized project claims with source and scope.")
        if any(not claim.source or not claim.scope for claim in allowed):
            questions.append("Add source and scope to every resume-eligible claim.")
        required_kinds = {"context": "a business context/difficulty Claim"}
        if not any(claim.kind in {"architecture", "control", "delivery"} for claim in allowed):
            questions.append("Provide an authorized architecture, control, or delivery Claim for the solution action.")
        for kind, description in required_kinds.items():
            if not any(claim.kind == kind for claim in allowed):
                questions.append(f"Provide {description}; business bullets cannot be invented.")
        if questions:
            results.append(ProbeProject(id=project.id, status="needs_user_input", questions=questions))
        else:
            results.append(ProbeProject(id=project.id, status="ready"))
    for index, employment in enumerate(profile.employment, 1):
        highlight_ids = [highlight.source_ingestion_id for highlight in employment.highlights]
        invalid_highlights = [
            highlight for highlight in employment.highlights
            if not 25 <= cjk_count(highlight.text) <= 35
        ]
        if (not 4 <= len(employment.highlights) <= 5
                or len(highlight_ids) != len(set(highlight_ids))
                or invalid_highlights):
            results.append(ProbeProject(
                id=f"employment-{index}",
                status="evidence_gate_blocked",
                questions=["Upload or confirm 4-5 distinct authorized work facts, each 25-35 CJK characters. The skill may only re-compose these approved facts into 40-50 CJK business bullets; it will not split a summary or invent details."],
            ))
    return results


def validate_employment_provenance(profile: Profile, inbox_path: Path | None) -> None:
    if not profile.employment:
        return
    if inbox_path is None or not inbox_path.is_file():
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: --inbox with approved ingestion records is required for employment rendering")
    inbox = load_yaml(inbox_path)
    entries = inbox.get("pending_ingestions", [])
    if not isinstance(entries, list):
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: ingestion inbox pending_ingestions must be a list")
    entry_ids = [str(entry.get("ingestion_id")) for entry in entries if isinstance(entry, dict) and entry.get("ingestion_id")]
    if len(entry_ids) != len(set(entry_ids)):
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: ingestion inbox contains duplicate ingestion IDs")
    approved = {
        str(entry.get("ingestion_id")): entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("status") == "approved" and entry.get("ingestion_id")
    }
    for employment in profile.employment:
        for highlight in employment.highlights:
            entry = approved.get(highlight.source_ingestion_id)
            if entry is None:
                raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: work highlight {highlight.source_ingestion_id!r} is not an approved inbox record")
            source_document = entry.get("source_document") if isinstance(entry, dict) else None
            source_hash = source_document.get("hash") if isinstance(source_document, dict) else None
            if not isinstance(source_hash, str) or source_hash != highlight.source_hash:
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: work highlight {highlight.source_ingestion_id!r} source hash is missing or changed"
                )


def approved_employment_sources(profile: Profile, inbox_path: Path) -> dict[str, dict[str, Any]]:
    """Return only approved, profile-selected inbox facts keyed by ingestion ID.

    A work bullet may rearrange bound verbatim fragments from these facts, but
    it never receives a summary or unapproved inbox data as a source.
    """
    inbox = load_yaml(inbox_path)
    entries = inbox.get("pending_ingestions", [])
    if not isinstance(entries, list):
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: ingestion inbox pending_ingestions must be a list")
    entry_ids = [str(entry.get("ingestion_id")) for entry in entries if isinstance(entry, dict) and entry.get("ingestion_id")]
    if len(entry_ids) != len(set(entry_ids)):
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: ingestion inbox contains duplicate ingestion IDs")
    approved = {
        str(entry.get("ingestion_id")): entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("status") == "approved" and entry.get("ingestion_id")
    }
    selected: dict[str, dict[str, Any]] = {}
    for employment in profile.employment:
        for highlight in employment.highlights:
            entry = approved.get(highlight.source_ingestion_id)
            if entry is None:
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: work source {highlight.source_ingestion_id!r} is not approved"
                )
            if str(entry.get("matched_employer", "")) != employment.employer:
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: work source {highlight.source_ingestion_id!r} belongs to another employer"
                )
            source_document = entry.get("source_document")
            if not isinstance(source_document, dict) or source_document.get("hash") != highlight.source_hash:
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: work source {highlight.source_ingestion_id!r} has a source-hash mismatch"
                )
            candidates = entry.get("candidate_data")
            if not isinstance(candidates, list) or not any(
                isinstance(candidate, dict) and candidate.get("text") == highlight.text
                for candidate in candidates
            ):
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: work source {highlight.source_ingestion_id!r} profile text is not an approved inbox fact"
                )
            selected[highlight.source_ingestion_id] = entry
    return selected


def _cjk_prefix(text: str, limit: int) -> str:
    """Return a verbatim prefix containing at most ``limit`` CJK characters."""
    if limit <= 0:
        return ""
    count = 0
    end = 0
    for index, char in enumerate(text):
        end = index + 1
        if CJK_RE.match(char):
            if count >= limit:
                end = index
                break
            count += 1
    return text[:end]


def _cjk_suffix(text: str, limit: int) -> str:
    """Return a verbatim suffix containing at most ``limit`` CJK characters."""
    if limit <= 0:
        return ""
    positions = [index for index, char in enumerate(text) if CJK_RE.match(char)]
    if len(positions) <= limit:
        return text
    return text[positions[-limit]:]


def _candidate_text(entry: dict[str, Any], kind: str | None = None) -> str:
    candidates = entry.get("candidate_data", [])
    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get("text"):
            continue
        if kind is None or candidate.get("inferred_type") == kind:
            return str(candidate["text"])
    return ""


def _employment_segment(entry: dict[str, Any], kinds: tuple[str, ...], limit: int) -> tuple[str, str]:
    """Pick a source-backed fragment and its source kind without inventing text."""
    for kind in kinds:
        source_text = _candidate_text(entry, kind)
        if source_text:
            fragment = _cjk_prefix(source_text, limit)
            if fragment:
                return fragment, kind
    return "", ""


def generate_employment_typeset(profile: Profile, inbox_path: Path | None) -> list[dict[str, Any]]:
    """Create an initial Agent B work section from approved inbox facts only.

    This is deliberately deterministic and conservative: one bullet is
    generated per confirmed highlight, and each of its three business
    segments is a verbatim fragment from that highlight's approved source.
    It exists so a normal JD-driven invocation can produce a first candidate
    without requiring a hand-authored ``--agent-b-output`` file.
    """
    if not profile.employment:
        return []
    if inbox_path is None:
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: --inbox is required for automatic work-bullet generation")
    approved = approved_employment_sources(profile, inbox_path)
    result: list[dict[str, Any]] = []
    for index, employment in enumerate(profile.employment, 1):
        bullets: list[dict[str, Any]] = []
        for highlight in employment.highlights:
            entry = approved.get(highlight.source_ingestion_id)
            if entry is None:
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: approved work source {highlight.source_ingestion_id!r} is missing"
                )
            # Keep the terminal result long enough to carry its metric (when
            # available), then allocate the remaining budget to context and
            # action.  All fragments remain contiguous substrings of the
            # approved inbox candidate text.
            metric_text = _candidate_text(entry, "metric")
            result_source_kind = "metric" if metric_text else "delivery"
            if not metric_text:
                metric_text = _candidate_text(entry, "delivery") or _candidate_text(entry, "control") or _candidate_text(entry, "architecture")
            if not metric_text:
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: work source {highlight.source_ingestion_id!r} has no result-capable fact"
                )
            result_fragment = _cjk_suffix(metric_text, 22)
            difficulty_fragment, difficulty_kind = _employment_segment(entry, ("context",), 10)
            if not difficulty_fragment or difficulty_kind != "context":
                raise EvidenceGateError(
                    f"BUSINESS_CONTEXT_MISSING: work source {highlight.source_ingestion_id!r} has no approved business-context fact"
                )
            action_fragment, action_kind = _employment_segment(entry, ("architecture", "control", "delivery"), 10)
            if not action_fragment or action_kind not in BUSINESS_ACTION_KINDS:
                raise EvidenceGateError(
                    f"BUSINESS_ACTION_MISSING: work source {highlight.source_ingestion_id!r} has no approved business-action fact"
                )
            # Avoid duplicate/overlapping segments when a source stores the
            # same sentence under multiple inferred types.
            if action_fragment == difficulty_fragment:
                action_fragment, action_kind = _employment_segment(entry, ("architecture", "control", "delivery"), 14)
            pieces = [difficulty_fragment, action_fragment, result_fragment]
            if len(set(pieces)) != 3:
                # Distinct source-backed slices are required by the business
                # structure validator; fail closed rather than fabricate a
                # connective sentence.
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: work source {highlight.source_ingestion_id!r} cannot form distinct business segments"
                )
            text = "；".join(pieces)
            # If a short source leaves us below the normal 40–50 budget, extend
            # only the action fragment with a longer verbatim prefix.
            if cjk_count(text) < 40:
                needed = 40 - cjk_count(text)
                action_source = _candidate_text(entry, action_kind) or _candidate_text(entry)
                action_fragment = _cjk_prefix(action_source, cjk_count(action_fragment) + needed)
                pieces[1] = action_fragment
                text = "；".join(pieces)
            detected_terms = technical_term_positions(text)
            if len({term.lower() for term, _, _ in detected_terms}) > MAX_TECHNICAL_TERMS_PER_BULLET:
                raise EvidenceGateError(
                    f"TECHNICAL_TERM_OVERLOAD: generated work bullet contains more than {MAX_TECHNICAL_TERMS_PER_BULLET} technical terms"
                )
            action_start = text.find(action_fragment)
            action_end = action_start + len(action_fragment)
            if any(not (action_start <= start and end <= action_end) for _, start, end in detected_terms):
                raise EvidenceGateError(
                    "TECHNICAL_TERM_PLACEMENT_ERROR: generated work technical terms must stay in the action fragment"
                )
            if not 40 <= cjk_count(text) <= 50:
                raise RetryableContractError(
                    f"BULLET_LENGTH_ERROR: generated work bullet has {cjk_count(text)} CJK characters"
                )
            result_fragment = pieces[2]
            source_id = highlight.source_ingestion_id
            bullets.append({
                "text": text,
                "bold_phrases_used": [result_fragment],
                "terminal_bold_phrase": result_fragment,
                "source_ingestion_ids": [source_id],
                "assertions": [
                    {"text": pieces[0], "source_ingestion_id": source_id},
                    {"text": pieces[1], "source_ingestion_id": source_id},
                    {"text": result_fragment, "source_ingestion_id": source_id},
                ],
            })
        result.append({"id": f"employment-{index}", "bullets": bullets})
    return result


def default_agent_a(
    profile: Profile, template: Template, selection: ProjectSelection | None = None,
) -> ResumePlan:
    selection = selection or selection_for_template(template)
    by_id = {project.id: project for project in profile.projects}
    projects = []
    for project_id in selection.project_ids:
        project = by_id[project_id]
        claims = [claim for claim in project.claims if claim.allowed_for_resume]
        projects.append(ResumePlanProject(id=project.id, title=project.title, start=project.start, end=project.end, tags=project.tags, claim_ids=[claim.id for claim in claims], claims=claims))
    return ResumePlan(target_role=template.target_role, projects=projects, selection=selection)


def validate_agent_a(
    plan: ResumePlan, profile: Profile, template: Template,
    selection: ProjectSelection | None = None,
) -> None:
    selection = selection or plan.selection or selection_for_template(template)
    if plan.target_role != template.target_role:
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: Agent A target_role differs from the selected template")
    if plan.selection is not None and plan.selection != selection:
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: Agent A altered the resolved project selection")
    allowed_projects = set(selection.project_ids)
    private_projects = {project.id: project for project in profile.projects}
    if {project.id for project in plan.projects} != allowed_projects or len(plan.projects) != len({project.id for project in plan.projects}):
        raise ValueError("Agent A must select exactly the resolved project IDs")
    for project in plan.projects:
        private = private_projects[project.id]
        if (project.title, project.start, project.end, project.tags) != (
            private.title, private.start, private.end, private.tags,
        ):
            raise EvidenceGateError(
                f"EVIDENCE_GATE_BLOCKED: Agent A altered the authorized project header for {project.id}"
            )
        private_claims = {claim.id: claim for claim in private.claims if claim.allowed_for_resume}
        if not set(project.claim_ids).issubset(private_claims):
            raise ValueError(f"Agent A selected an unauthorized claim for {project.id}")
        for claim in project.claims:
            original = private_claims.get(claim.id)
            if original is None or claim != original:
                raise ValueError(f"Agent A altered claim {claim.id}; claims must remain source-identical")


def validate_agent_b(copy: TypesetPlan, plan: ResumePlan, *, require_project_stages: bool = False) -> None:
    min_chars, max_chars = CONTENT_BOUNDS[copy.content_mode]

    def enforce_budget(text: str, label: str) -> None:
        count = cjk_count(text)
        if not min_chars <= count <= max_chars:
            raise RetryableContractError(
                f"BULLET_LENGTH_ERROR: {label} has {count} CJK characters; "
                f"{copy.content_mode} budget is {min_chars}-{max_chars}"
            )

    plan_projects = {project.id: project for project in plan.projects}
    if {project.id for project in copy.projects} != set(plan_projects) or len(copy.projects) != len({project.id for project in copy.projects}):
        raise ValueError("Agent B project IDs must exactly match Agent A")
    for project in copy.projects:
        claims = {claim.id: claim for claim in plan_projects[project.id].claims}
        def referenced(ids: list[str]) -> list[Claim]:
            if not set(ids).issubset(claims):
                raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: {project.id} references an unauthorized or cross-project Claim")
            return [claims[claim_id] for claim_id in ids]

        def one_claim_supports(items: list[Claim], text: str, kind: str | None = None) -> bool:
            return any((kind is None or claim.kind == kind) and text in claim.text for claim in items)

        def validate_assertions(
            rendered: str, assertions: list[AssertionFragment], items: list[Claim], label: str,
            derived_phrase: str | None = None,
        ) -> None:
            """Bind every semantic character to a single source Claim.

            Punctuation may be introduced while composing a sentence.  All
            other characters -- including ASCII, full-width digits, English
            terms and percent symbols used as facts -- must sit inside at
            least one declared verbatim assertion.  This closes the old
            CJK-only coverage hole (for example an injected ``999%``).
            """
            covered = [False] * len(rendered)
            for assertion in assertions:
                claim = claims.get(assertion.source_claim_id)
                if claim is None or claim not in items:
                    raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: {project.id} {label} assertion crosses Claim scope")
                if assertion.text not in claim.text or assertion.text not in rendered:
                    raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: {project.id} {label} assertion lacks verbatim Claim support")
                start = rendered.find(assertion.text)
                while start >= 0:
                    for position in range(start, start + len(assertion.text)):
                        covered[position] = True
                    start = rendered.find(assertion.text, start + 1)
            # A derived metric is intentionally not a verbatim source phrase;
            # its own formula/input gate below is the evidence for this span.
            if derived_phrase:
                start = rendered.find(derived_phrase)
                while start >= 0:
                    for position in range(start, start + len(derived_phrase)):
                        covered[position] = True
                    start = rendered.find(derived_phrase, start + 1)
            unbound = "".join(
                char for index, char in enumerate(rendered)
                if not covered[index] and not char.isspace() and char not in UNBOUND_PUNCTUATION
            )
            if unbound:
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: {project.id} {label} contains unbound assertion text {unbound!r}"
                )

        if project.overview is not None:
            overview_claims = referenced(project.overview.source_claim_ids)
            overview = project.overview.text
            validate_assertions(overview, project.overview.assertions, overview_claims, "overview")
            for term in OWNERSHIP_TERMS:
                if term in overview and not one_claim_supports(overview_claims, term):
                    raise EvidenceGateError(f"OVERVIEW_EVIDENCE_MISMATCH: {project.id} ownership term {term!r} lacks one-Claim support")
            for term in ARCHITECTURE_TERMS:
                if term in overview and not one_claim_supports(overview_claims, term, "architecture"):
                    raise EvidenceGateError(f"OVERVIEW_EVIDENCE_MISMATCH: {project.id} architecture term {term!r} lacks architecture Claim support")
            for term in set(re.findall(r"[0-9０-９]+(?:[.%％][0-9０-９]*)?|提升|降低", overview)):
                if not one_claim_supports(overview_claims, term, "metric"):
                    raise EvidenceGateError(f"OVERVIEW_EVIDENCE_MISMATCH: {project.id} metric term {term!r} lacks metric Claim support")

        staged_bullets = [bullet for bullet in project.bullets if isinstance(bullet, ProjectStageBullet)]
        if require_project_stages and not staged_bullets:
            raise EvidenceGateError(
                f"EVIDENCE_GATE_BLOCKED: {project.id} must use exactly three project-stage bullets (background, solution, result)"
            )
        if staged_bullets:
            # New format: exactly one project-level background, solution and
            # result bullet.  The background is not duplicated in each point.
            for stage_bullet in staged_bullets:
                enforce_budget(stage_bullet.text, f"{project.id} {stage_bullet.stage} bullet")
                validate_project_business_readability(
                    stage_bullet.text,
                    stage_bullet.stage,
                    f"{project.id} {stage_bullet.stage} bullet",
                )
                stage_claims = referenced(stage_bullet.source_claim_ids)
                validate_assertions(stage_bullet.text, stage_bullet.assertions, stage_claims, f"{stage_bullet.stage} bullet", derived_phrase=stage_bullet.terminal_bold_phrase if stage_bullet.derived_metric else None)
                expected_kinds = {
                    "background": {"context"},
                    "solution": {"architecture", "control", "delivery"},
                    "result": {"metric"},
                }[stage_bullet.stage]
                if not any(claim.kind in expected_kinds for claim in stage_claims):
                    raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: {project.id} {stage_bullet.stage} bullet lacks required Claim kind")
                for phrase in stage_bullet.bold_phrases_used:
                    if stage_bullet.derived_metric is not None and phrase == stage_bullet.text:
                        continue
                    if not one_claim_supports(stage_claims, phrase):
                        raise EvidenceGateError(f"BULLET_BOLD_MISSING_ERROR: {project.id} {stage_bullet.stage} phrase {phrase!r} lacks one-Claim source support")
                if stage_bullet.stage != "result":
                    continue
                for token in [*NUMERIC_TOKEN_RE.findall(stage_bullet.text), *(term for term in METRIC_EFFECT_TERMS if term in stage_bullet.text)]:
                    if stage_bullet.derived_metric and token in stage_bullet.text:
                        continue
                    if not one_claim_supports(stage_claims, token, "metric"):
                        raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: {project.id} result metric token {token!r} lacks one metric Claim")
                if stage_bullet.derived_metric:
                    validate_derived_metric(metric=stage_bullet.derived_metric, result_text=stage_bullet.terminal_bold_phrase or "", claims=claims, declared_claim_ids=stage_bullet.source_claim_ids, label=f"{project.id} result bullet")
                elif not one_claim_supports(stage_claims, stage_bullet.terminal_bold_phrase or "", "metric"):
                    raise EvidenceGateError(f"TERMINAL_BOLD_ERROR: {project.id} result terminal phrase requires a metric Claim")
            continue

        if project.overview is None:
            raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: {project.id} legacy project is missing overview")
        overview_claims = referenced(project.overview.source_claim_ids)
        overview = project.overview.text
        validate_assertions(overview, project.overview.assertions, overview_claims, "overview")
        for term in OWNERSHIP_TERMS:
            if term in overview and not one_claim_supports(overview_claims, term):
                raise EvidenceGateError(f"OVERVIEW_EVIDENCE_MISMATCH: {project.id} ownership term {term!r} lacks one-Claim support")
        for term in ARCHITECTURE_TERMS:
            if term in overview and not one_claim_supports(overview_claims, term, "architecture"):
                raise EvidenceGateError(f"OVERVIEW_EVIDENCE_MISMATCH: {project.id} architecture term {term!r} lacks architecture Claim support")
        for term in set(re.findall(r"[0-9０-９]+(?:[.%％][0-9０-９]*)?|提升|降低", overview)):
            if not one_claim_supports(overview_claims, term, "metric"):
                raise EvidenceGateError(f"OVERVIEW_EVIDENCE_MISMATCH: {project.id} metric term {term!r} lacks metric Claim support")
        for bullet in project.bullets:
            enforce_budget(bullet.text, f"{project.id} bullet")
            bullet_claims = referenced(bullet.source_claim_ids)
            structure = bullet.business_structure
            derived_metric = structure.quantified_result.derived_metric if structure is not None else None
            if not has_metric_assertion(bullet.text):
                raise NeedsUserInputError(
                    f"NEEDS_USER_INPUT: {project.id} project result must contain an authorized numeric or derived efficiency metric"
                )
            validate_assertions(
                bullet.text, bullet.assertions, bullet_claims, "bullet",
                derived_phrase=structure.quantified_result.text if derived_metric else None,
            )
            for label, segment in (
                ("business_difficulty", structure.business_difficulty),
                ("solution_action", structure.solution_action),
                ("quantified_result", structure.quantified_result),
            ):
                if label == "quantified_result" and segment.derived_metric is not None:
                    if segment.source_claim_id not in segment.derived_metric.source_claim_ids:
                        raise EvidenceGateError(
                            f"EVIDENCE_GATE_BLOCKED: {project.id} quantified_result derived source mismatch"
                        )
                    # The calculated phrase is validated against its input
                    # Claim and formula below; it is not required verbatim.
                    continue
                claim = claims.get(segment.source_claim_id)
                if claim is None or claim not in bullet_claims:
                    raise EvidenceGateError(
                        f"EVIDENCE_GATE_BLOCKED: {project.id} {label} references an unauthorized or cross-project Claim"
                    )
                if segment.text not in bullet.text or segment.text not in claim.text:
                    raise EvidenceGateError(
                        f"EVIDENCE_GATE_BLOCKED: {project.id} {label} lacks one-Claim verbatim support"
                    )
                expected_kinds = {
                    "business_difficulty": {"context"},
                    "solution_action": {"architecture", "control", "delivery"},
                    "quantified_result": result_kinds_for(bullet.text),
                }[label]
                if claim.kind not in expected_kinds:
                    raise EvidenceGateError(
                        f"EVIDENCE_GATE_BLOCKED: {project.id} {label} requires Claim kind {sorted(expected_kinds)}"
                    )
            for phrase in bullet.bold_phrases_used:
                if derived_metric and phrase == structure.quantified_result.text:
                    continue
                if not one_claim_supports(bullet_claims, phrase):
                    raise EvidenceGateError(f"BULLET_BOLD_MISSING_ERROR: {phrase!r} lacks one-Claim source support")
            if not (derived_metric and bullet.terminal_bold_phrase == structure.quantified_result.text) \
                    and not one_claim_supports(bullet_claims, bullet.terminal_bold_phrase):
                raise EvidenceGateError("TERMINAL_BOLD_ERROR: terminal phrase lacks one-Claim source support")
            if has_metric_assertion(bullet.text):
                metric_tokens = [*NUMERIC_TOKEN_RE.findall(bullet.text), *(term for term in METRIC_EFFECT_TERMS if term in bullet.text)]
                for token in metric_tokens:
                    if derived_metric and token in structure.quantified_result.text:
                        continue
                    if not one_claim_supports(bullet_claims, token, "metric"):
                        raise EvidenceGateError(
                            f"EVIDENCE_GATE_BLOCKED: {project.id} metric token {token!r} lacks one metric Claim"
                        )
                if derived_metric:
                    validate_derived_metric(
                        metric=derived_metric,
                        result_text=bullet.terminal_bold_phrase,
                        claims=claims,
                        declared_claim_ids=bullet.source_claim_ids,
                        label=f"{project.id} bullet",
                    )
                elif not one_claim_supports(bullet_claims, bullet.terminal_bold_phrase, "metric"):
                    raise EvidenceGateError("TERMINAL_BOLD_ERROR: terminal numeric result requires a metric Claim")
            elif not any(
                one_claim_supports(bullet_claims, bullet.terminal_bold_phrase, kind)
                for kind in result_kinds_for(bullet.text)
            ):
                raise EvidenceGateError(
                    "TERMINAL_BOLD_ERROR: non-numeric terminal conclusion requires an architecture, control, or delivery Claim"
                )


def validate_typeset_employment(
    copy: TypesetPlan, profile: Profile, inbox_path: Path | None,
    *, content_mode: str | None = None,
) -> None:
    """Validate Agent B work-bullet re-composition against approved inbox facts."""
    mode = content_mode or copy.content_mode
    if mode not in CONTENT_BOUNDS:
        raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: unsupported content_mode {mode!r}")
    min_chars, max_chars = CONTENT_BOUNDS[mode]
    expected = {f"employment-{index}" for index, _ in enumerate(profile.employment, 1)}
    rendered = [item.id for item in copy.employment]
    if set(rendered) != expected or len(rendered) != len(set(rendered)):
        raise EvidenceGateError(
            "EVIDENCE_GATE_BLOCKED: Agent B must provide exactly one work-bullet set for every employment entry"
        )
    if not profile.employment:
        return
    if inbox_path is None:
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: --inbox is required to validate work-bullet sources")
    approved_sources = approved_employment_sources(profile, inbox_path)
    by_id = {item.id: item for item in copy.employment}

    for index, employment in enumerate(profile.employment, 1):
        employment_id = f"employment-{index}"
        source_ids = {highlight.source_ingestion_id for highlight in employment.highlights}
        used_source_ids: set[str] = set()
        rendered_employment = by_id[employment_id]
        if len(rendered_employment.bullets) != len(source_ids):
            raise EvidenceGateError(
                f"EVIDENCE_GATE_BLOCKED: {employment_id} must render its exactly {len(source_ids)} approved work facts"
            )
        if len({bullet.text for bullet in rendered_employment.bullets}) != len(rendered_employment.bullets):
            raise EvidenceGateError(
                f"EVIDENCE_GATE_BLOCKED: {employment_id} repeats a rendered work bullet instead of presenting distinct approved facts"
            )

        def source_supports(source_id: str, text: str, kind: str | None = None) -> bool:
            entry = approved_sources.get(source_id)
            if entry is None or source_id not in source_ids:
                return False
            candidates = entry.get("candidate_data", [])
            return any(
                isinstance(candidate, dict)
                and (kind is None or candidate.get("inferred_type") == kind)
                and text in str(candidate.get("text", ""))
                for candidate in candidates
            )

        for bullet in rendered_employment.bullets:
            count = cjk_count(bullet.text)
            # Employment highlights retain the 40–50 contract even when a
            # sparse-page recovery widens project bullets.  Work facts are a
            # separate, user-confirmed source pool and must not be silently
            # rewritten just to fill vertical space.
            work_min_chars, work_max_chars = CONTENT_BOUNDS["normal"]
            if not work_min_chars <= count <= work_max_chars:
                raise RetryableContractError(
                    f"BULLET_LENGTH_ERROR: {employment_id} work bullet has {count} CJK characters; "
                    f"normal budget is {work_min_chars}-{work_max_chars}"
                )
            declared_sources = set(bullet.source_ingestion_ids)
            if not declared_sources or not declared_sources.issubset(source_ids):
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: {employment_id} bullet references an unapproved or foreign work source"
                )
            structure = bullet.business_structure
            derived_metric = structure.quantified_result.derived_metric if structure is not None else None
            covered = [False] * len(bullet.text)
            for assertion in bullet.assertions:
                if assertion.source_ingestion_id not in declared_sources or not source_supports(
                    assertion.source_ingestion_id, assertion.text
                ) or assertion.text not in bullet.text:
                    raise EvidenceGateError(
                        f"EVIDENCE_GATE_BLOCKED: {employment_id} work assertion lacks verbatim approved-source support"
                    )
                used_source_ids.add(assertion.source_ingestion_id)
                start = bullet.text.find(assertion.text)
                while start >= 0:
                    for position in range(start, start + len(assertion.text)):
                        covered[position] = True
                    start = bullet.text.find(assertion.text, start + 1)
            if derived_metric:
                start = bullet.text.find(structure.quantified_result.text)
                while start >= 0:
                    for position in range(start, start + len(structure.quantified_result.text)):
                        covered[position] = True
                    start = bullet.text.find(structure.quantified_result.text, start + 1)
                used_source_ids.add(assertion.source_ingestion_id)
            unbound = "".join(
                char for position, char in enumerate(bullet.text)
                if not covered[position] and not char.isspace() and char not in UNBOUND_PUNCTUATION
            )
            if unbound:
                raise EvidenceGateError(
                    f"EVIDENCE_GATE_BLOCKED: {employment_id} work bullet contains unbound assertion text {unbound!r}"
                )
            validate_work_business_readability(
                bullet.text,
                bullet.assertions,
                source_supports,
                declared_sources,
                f"{employment_id} work bullet",
            )
            if structure is not None:
                for label, segment in (
                    ("business_difficulty", structure.business_difficulty),
                    ("solution_action", structure.solution_action),
                    ("quantified_result", structure.quantified_result),
                ):
                    if label == "quantified_result" and segment.derived_metric is not None:
                        # The displayed result is calculated, not a verbatim
                        # inbox phrase.  Its inputs are checked below by the
                        # deterministic formula gate.
                        if segment.source_ingestion_id not in declared_sources:
                            raise EvidenceGateError(
                                f"EVIDENCE_GATE_BLOCKED: {employment_id} quantified_result derived source is not declared"
                            )
                        used_source_ids.update(segment.derived_metric.source_claim_ids)
                        continue
                    if segment.source_ingestion_id not in declared_sources or not source_supports(
                        segment.source_ingestion_id, segment.text
                    ):
                        raise EvidenceGateError(
                            f"EVIDENCE_GATE_BLOCKED: {employment_id} {label} lacks verbatim approved-source support"
                        )
                    used_source_ids.add(segment.source_ingestion_id)
                    expected_kinds = {
                        "business_difficulty": {"context"},
                        "solution_action": {"architecture", "control", "delivery"},
                        "quantified_result": result_kinds_for(bullet.text),
                    }[label]
                    if not any(
                        source_supports(segment.source_ingestion_id, segment.text, kind)
                        for kind in expected_kinds
                    ):
                        raise EvidenceGateError(
                            f"EVIDENCE_GATE_BLOCKED: {employment_id} {label} requires approved source kind {sorted(expected_kinds)}"
                        )
                result = structure.quantified_result
                if derived_metric:
                    validate_derived_employment_metric(
                        metric=derived_metric,
                        result_text=bullet.terminal_bold_phrase,
                        declared_source_ids=declared_sources,
                        source_supports=source_supports,
                        label=f"{employment_id} bullet",
                    )
                elif not any(
                    source_supports(result.source_ingestion_id, result.text, kind)
                    for kind in result_kinds_for(bullet.text)
                ):
                    source_kinds = "metric" if has_metric_assertion(bullet.text) else "architecture, control, or delivery"
                    raise EvidenceGateError(
                        f"TERMINAL_BOLD_ERROR: {employment_id} terminal result requires a {source_kinds} source"
                    )
            else:
                # New direct work-bullet format: assertions are source-bound
                # above; only the terminal outcome needs a matching source
                # kind.  No project-style labels are inferred or required.
                terminal_kinds = result_kinds_for(bullet.text)
                if not any(
                    source_supports(source_id, bullet.terminal_bold_phrase, kind)
                    for source_id in declared_sources
                    for kind in terminal_kinds
                ):
                    source_kinds = "metric" if has_metric_assertion(bullet.text) else "architecture, control, or delivery"
                    raise EvidenceGateError(
                        f"TERMINAL_BOLD_ERROR: {employment_id} terminal result requires a {source_kinds} source"
                    )
            for phrase in [*bullet.bold_phrases_used, bullet.terminal_bold_phrase]:
                if structure is not None and derived_metric and phrase == result.text:
                    continue
                if not any(source_supports(source_id, phrase) for source_id in declared_sources):
                    raise EvidenceGateError(
                        f"BULLET_BOLD_MISSING_ERROR: {employment_id} bold phrase {phrase!r} lacks one approved source"
                    )
            if has_metric_assertion(bullet.text):
                for token in [*NUMERIC_TOKEN_RE.findall(bullet.text), *(term for term in METRIC_EFFECT_TERMS if term in bullet.text)]:
                    if derived_metric and token in result.text:
                        continue
                    if not any(source_supports(source_id, token, "metric") for source_id in declared_sources):
                        raise EvidenceGateError(
                            f"EVIDENCE_GATE_BLOCKED: {employment_id} metric token {token!r} lacks one approved metric source"
                        )
        if used_source_ids != source_ids:
            raise EvidenceGateError(
                f"EVIDENCE_GATE_BLOCKED: {employment_id} must use every approved work fact at least once"
            )


def load_and_validate_render_inputs(
    *, profile_path: Path, template_path: Path, resume_plan_path: Path,
    typeset_plan_path: Path, inbox_path: Path | None,
    jd_brief_path: Path | None = None, jd_evidence_map_path: Path | None = None,
) -> tuple[Profile, Template, ResumePlan, TypesetPlan]:
    """The only admission path for content that is about to reach a renderer.

    This is intentionally independent from the CLI orchestration so a direct
    ``typst_renderer.py`` invocation cannot bypass the evidence gate.
    """
    profile = Profile.model_validate(load_yaml(profile_path))
    template = Template.model_validate(load_yaml(template_path))
    if profile.identity.market != template.market:
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: profile and template market routes differ")
    selection = resolve_project_selection(
        profile=profile, template=template, jd_brief_path=jd_brief_path,
        jd_evidence_map_path=jd_evidence_map_path,
    )
    validate_employment_provenance(profile, inbox_path)
    probe = data_probe(profile, template, selection)
    blocking = [item for item in probe if item.status in {"needs_user_input", "evidence_gate_blocked"}]
    if blocking:
        raise EvidenceGateError(
            f"EVIDENCE_GATE_BLOCKED: Data Probe has unresolved entries: {[item.id for item in blocking]}"
        )
    resume_plan = ResumePlan.model_validate(load_json(resume_plan_path))
    validate_agent_a(resume_plan, profile, template, selection)
    typeset_plan = TypesetPlan.model_validate(load_json(typeset_plan_path))
    validate_agent_b(typeset_plan, resume_plan, require_project_stages=True)
    validate_typeset_employment(typeset_plan, profile, inbox_path)
    return profile, template, resume_plan, typeset_plan


def write_json(path: Path, payload: BaseModel | list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, list):
        data = [item.model_dump(mode="json") for item in payload]
    else:
        data = payload.model_dump(mode="json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_retryable_contract_failure(exc: Exception) -> bool:
    text = str(exc)
    return any(code in text for code in (
        "BULLET_LENGTH_ERROR", "BULLET_BOLD_MISSING_ERROR",
        "TERMINAL_BOLD_ERROR", "BUSINESS_BULLET_STRUCTURE_ERROR", "missing", "Field required",
    ))


def validate_agent_b_attempts(
    paths: list[Path], plan: ResumePlan, profile: Profile, inbox_path: Path | None, output_dir: Path,
) -> TypesetPlan:
    """Accept one initial Agent B draft plus at most two contract-only rewrites."""
    if not 1 <= len(paths) <= 3:
        raise ValueError("AGENT_B_ATTEMPT_ERROR: provide an initial output and at most two retry outputs")
    attempts: list[dict[str, Any]] = []
    for attempt, path in enumerate(paths, 1):
        try:
            candidate = TypesetPlan.model_validate(load_json(path))
            validate_agent_b(candidate, plan, require_project_stages=True)
            validate_typeset_employment(candidate, profile, inbox_path)
        except NeedsUserInputError:
            raise
        except EvidenceGateError:
            raise
        except (ValidationError, ValueError) as exc:
            retryable = is_retryable_contract_failure(exc)
            attempts.append({"attempt": attempt, "path": str(path), "status": "retryable_contract_error" if retryable else "evidence_gate_blocked", "error": str(exc)})
            if not retryable:
                raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: {exc}") from exc
            continue
        attempts.append({"attempt": attempt, "path": str(path), "status": "accepted"})
        (output_dir / "agent-b-attempts.json").write_text(json.dumps({"attempts": attempts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return candidate
    (output_dir / "agent-b-attempts.json").write_text(json.dumps({"attempts": attempts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise ValueError("NEEDS_USER_INPUT: Agent B exhausted initial generation plus two allowed contract retries")


LAYOUT_STATES: dict[str, dict[str, float]] = {
    "normal": {
        "header_to_first_module": 10.0, "module_gap": 7.0, "project_gap": 8.0,
        "title_to_overview": 4.0, "overview_to_bullet": 3.0,
    },
    "compact_1": {
        "header_to_first_module": 8.0, "module_gap": 5.0, "project_gap": 6.0,
        "title_to_overview": 3.0, "overview_to_bullet": 2.0,
    },
    "compact_2": {
        "header_to_first_module": 6.0, "module_gap": 4.0, "project_gap": 5.0,
        "title_to_overview": 2.0, "overview_to_bullet": 1.0,
    },
}

RENDER_FAILURE_CODES = (
    "PARAGRAPH_SPACING_ERROR", "PAGE_SIZE_ERROR", "MARGIN_OUT_OF_RANGE_ERROR",
    "MULTI_COLUMN_LAYOUT_ERROR", "VISUAL_DESIGN_MISMATCH_ERROR",
    "FONT_TOO_SMALL_ERROR", "GEOMETRY_QA_ERROR", "COMPLIANCE_PHOTO_ERROR",
    "PROVENANCE_HASH_ERROR", "REFLOW_SOURCE_MUTATION_ERROR",
    "REFLOW_THEME_MUTATION_ERROR", "REFLOW_INPUT_MUTATION_ERROR",
)


def renderer_failure_details(exc: subprocess.CalledProcessError) -> tuple[str, str]:
    """Preserve the renderer's actionable gate code instead of flattening it.

    The subprocess traceback is the only diagnostic available when Typst or
    an artifact gate fails before geometry-qa.json exists.  Extracting the
    first known code keeps SkillOpt routing deterministic while the trace
    still records a short, non-sensitive detail string.
    """
    output = "\n".join(part for part in (exc.stdout, exc.stderr) if part)
    code = next((candidate for candidate in RENDER_FAILURE_CODES if candidate in output), "RENDER_ERROR")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    detail = lines[-1] if lines else f"renderer exited with code {exc.returncode}"
    return code, detail[-1000:]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_layout_vars(output_dir: Path, state: str, round_number: int, previous: dict[str, Any] | None, action: str) -> Path:
    payload = {
        "reflow_round": round_number,
        "layout_state": state,
        "spacing": LAYOUT_STATES[state],
        "feedback_trace": {
            "previous_page_count": previous.get("page_count") if previous else None,
            "previous_bottom_whitespace_pt": previous.get("bottom_whitespace_pt") if previous else None,
            "action_taken": action,
        },
    }
    path = output_dir / "layout_vars.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def parse_geometry(path: Path) -> dict[str, Any]:
    payload = load_json(path)
    findings = payload.get("findings", [])
    page_count = next((item.get("observed") for item in findings if item.get("code") == "PAGE_COUNT_ERROR"), 1)
    bottom = next((item.get("observed", {}).get("pt") for item in findings if item.get("code") == "BOTTOM_WHITESPACE_EXCESS"), None)
    if bottom is None and isinstance(payload.get("bottom_whitespace_pt"), (int, float)):
        bottom = float(payload["bottom_whitespace_pt"])
    return {"page_count": page_count, "bottom_whitespace_pt": bottom, "findings": findings,
            "qa_measurements": payload.get("qa_measurements", {})}


def append_content_recovery_trace(staging: Path, entry: dict[str, Any]) -> None:
    """Append, rather than overwrite, each bounded content recovery action.

    A single overfull run may legitimately compress bullets and then prune a
    whole project.  Keeping every action in one trace prevents the later
    action from hiding the earlier SkillOpt recovery attempt.
    """
    path = staging / "content-recovery-trace.json"
    attempts: list[dict[str, Any]] = []
    if path.is_file():
        try:
            previous = load_json(path)
            raw_attempts = previous.get("attempts")
            if isinstance(raw_attempts, list):
                attempts = [item for item in raw_attempts if isinstance(item, dict)]
            elif isinstance(previous, dict) and previous.get("status"):
                attempts = [previous]
        except (OSError, ValueError, TypeError):
            attempts = []
    attempt = dict(entry)
    attempt["attempt"] = len(attempts) + 1
    attempts.append(attempt)
    payload = dict(attempt)
    payload["attempts"] = attempts
    atomic_write_json(path, payload)


def _content_recovery_candidate(
    staging: Path, args: argparse.Namespace, template: Template,
) -> tuple[dict[str, Any], ResumePlan, Path | None] | None:
    """Build one evidence-only recovery candidate, adding a ranked project when possible."""
    try:
        from skillopt_auto_loop import build_typeset_candidate
        original = load_json(staging / "typeset-plan.json")
        plan = load_json(staging / "resume-plan.json")
        recovery_brief: Path | None = None
        plan_model = ResumePlan.model_validate(plan)
        # Sparse pages may safely add one more JD-ranked, authorized project.
        # This is a content recovery override, not a permanent change to the
        # user's JD brief; the copied brief lives only in staging.
        if len(plan_model.projects) < 4 and args.jd_brief and args.jd_evidence_map:
            brief = load_json(args.jd_brief)
            if int(brief.get("max_projects", 3)) < 4:
                brief["max_projects"] = 4
                recovery_brief = staging / "jd-brief-content-recovery.json"
                atomic_write_json(recovery_brief, brief)
                recovery_selection = resolve_project_selection(
                    profile=Profile.model_validate(load_yaml(args.profile)),
                    template=template,
                    jd_brief_path=recovery_brief,
                    jd_evidence_map_path=args.jd_evidence_map,
                )
                if len(recovery_selection.project_ids) > len(plan_model.projects):
                    profile = Profile.model_validate(load_yaml(args.profile))
                    plan_model = default_agent_a(profile, template, recovery_selection)
                    plan = plan_model.model_dump(mode="json")
        # Sparse-page recovery may widen each project bullet to the explicit
        # 50–130 CJK budget.  The normal first render remains 40–50; this mode
        # is stamped into the candidate and re-checked by every renderer.
        candidate = build_typeset_candidate(plan, original, content_mode="expanded")
        if candidate is None or candidate == original:
            return None
        validated_plan = ResumePlan.model_validate(plan)
        validated_candidate = TypesetPlan.model_validate(candidate)
        validate_agent_b(validated_candidate, validated_plan, require_project_stages=True)
        # Never replace a draft with a shorter recovery candidate.  Sparse-page
        # recovery is allowed to add/restore Claim-backed text only.
        original_chars = sum(cjk_count(str(b.get("text", ""))) for p in original.get("projects", []) for b in p.get("bullets", []) if isinstance(b, dict))
        candidate_chars = sum(cjk_count(str(b.get("text", ""))) for p in candidate.get("projects", []) for b in p.get("bullets", []) if isinstance(b, dict))
        if candidate_chars < original_chars:
            return None
        return candidate, validated_plan, recovery_brief
    except (OSError, ValueError, ValidationError, TypeError):
        return None


def _content_prune_candidate(
    staging: Path,
) -> tuple[dict[str, Any], ResumePlan, str] | None:
    """Prepare an evidence-preserving whole-project prune after compact_2.

    Project removal is deliberately coarse-grained: the lowest-ranked project
    is removed from both Agent A and Agent B outputs, while every retained
    Claim and bullet remains byte-for-byte unchanged.  This is the only
    automatic overfull recovery; deleting individual facts would silently
    alter the candidate's evidence surface.
    """
    try:
        plan_payload = load_json(staging / "resume-plan.json")
        typeset_payload = load_json(staging / "typeset-plan.json")
        plan = ResumePlan.model_validate(plan_payload)
        if len(plan.projects) <= 3:
            return None
        remove_id = plan.projects[-1].id
        remaining_projects = [project for project in plan.projects if project.id != remove_id]
        remaining_typeset = [
            project for project in typeset_payload.get("projects", [])
            if isinstance(project, dict) and str(project.get("id")) != remove_id
        ]
        if len(remaining_typeset) != len(remaining_projects):
            return None
        candidate_plan_payload = plan.model_copy(update={
            "projects": remaining_projects,
            "selection": (
                plan.selection.model_copy(update={
                    "project_ids": [project.id for project in remaining_projects],
                }) if plan.selection is not None else None
            ),
        }).model_dump(mode="json")
        candidate_plan = ResumePlan.model_validate(candidate_plan_payload)
        candidate_typeset = {**typeset_payload, "projects": remaining_typeset}
        candidate_typeset_model = TypesetPlan.model_validate(candidate_typeset)
        validate_agent_b(candidate_typeset_model, candidate_plan, require_project_stages=True)
        return candidate_typeset, candidate_plan, remove_id
    except (OSError, ValueError, ValidationError, TypeError):
        return None


def _content_compress_candidate(
    staging: Path,
) -> dict[str, Any] | None:
    """Compose a shorter 30–40 CJK candidate after compact_2 remains overfull."""
    try:
        from skillopt_auto_loop import build_typeset_candidate
        plan = load_json(staging / "resume-plan.json")
        original = load_json(staging / "typeset-plan.json")
        candidate = build_typeset_candidate(plan, original, content_mode="compressed")
        if candidate is None or candidate == original:
            return None
        validated_plan = ResumePlan.model_validate(plan)
        validated_candidate = TypesetPlan.model_validate(candidate)
        validate_agent_b(validated_candidate, validated_plan, require_project_stages=True)
        original_chars = sum(
            cjk_count(str(b.get("text", "")))
            for p in original.get("projects", [])
            for b in p.get("bullets", []) if isinstance(b, dict)
        )
        candidate_chars = sum(
            cjk_count(str(b.get("text", "")))
            for p in candidate.get("projects", [])
            for b in p.get("bullets", []) if isinstance(b, dict)
        )
        if candidate_chars >= original_chars:
            return None
        return candidate
    except (OSError, ValueError, ValidationError, TypeError):
        return None


def _parse_project_dir_arg(value: str) -> tuple[str, Path]:
    """Parse the CLI ``PROJECT_ID=/absolute/path`` convenience input."""
    project_id, separator, raw_path = value.partition("=")
    if not separator or not project_id.strip() or not raw_path.strip():
        raise ValueError("--project-dir must be PROJECT_ID=/absolute/or/local/path")
    root = Path(raw_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    return project_id.strip(), root


def quarantine_build_failure(
    output_dir: Path, preflight_run_id: str | None, preflight_dir: Path | None,
    *, code: str, detail: str, phase: str,
) -> Path:
    """Quarantine a failure even when the caller did not request rendering.

    Plan-only invocations historically printed validation errors without
    creating a failure event, which made the SkillOpt controller appear to be
    bypassed.  Give every failed build a transaction-owned, metadata-only
    quarantine record.  This helper never moves an existing formal artifact
    and never creates a delivery manifest.
    """
    if preflight_dir is not None and preflight_dir.exists():
        return quarantine_render_transaction(
            output_dir, preflight_run_id or preflight_dir.name, preflight_dir,
            code=code, detail=detail, phase=phase,
        )
    run_id, staging = begin_render_transaction(output_dir)
    atomic_write_json(staging / "build-failure.json", {
        "status": "failed", "code": code, "phase": phase, "detail": detail,
    })
    return quarantine_render_transaction(
        output_dir, run_id, staging, code=code, detail=detail, phase=phase,
    )


def _prepare_jd_evidence_map(args: argparse.Namespace, work_dir: Path) -> None:
    """Generate a source-backed JD map when callers provide project paths.

    This keeps the public build command useful for the intended workflow
    (JD + local project folders) while preserving the same lexical, read-only
    scanner and hash verification used by the explicit map path.
    """
    if not args.project_dir:
        return
    if not args.jd_brief:
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: --project-dir requires --jd-brief")
    if args.jd_evidence_map:
        raise EvidenceGateError("EVIDENCE_GATE_BLOCKED: use either --project-dir or --jd-evidence-map, not both")
    try:
        from jd_project_selector import parse_project_dir, scan

        project_dirs = [parse_project_dir(value) for value in args.project_dir]
        brief = JDBrief.model_validate(load_json(args.jd_brief))
        generated = scan(brief, project_dirs)
        generated_path = work_dir / "jd-evidence-map.generated.json"
        atomic_write_json(generated_path, generated.model_dump(mode="json"))
        args.jd_evidence_map = generated_path
    except (OSError, TypeError, ValueError, argparse.ArgumentTypeError) as exc:
        raise EvidenceGateError(f"EVIDENCE_GATE_BLOCKED: local JD project scan failed: {exc}") from exc


def render_with_reflow(args: argparse.Namespace, template: Template, theme_vars: Path,
                       *, prepared_dir: Path | None = None, prepared_run_id: str | None = None,
                       expansion_attempt: int = 0, compression_attempt: int = 0,
                       prune_attempt: int = 0) -> dict[str, Any]:
    """Render each state in isolation and publish only an eligible transaction."""
    if prepared_dir is None:
        run_id, staging = begin_render_transaction(args.output_dir)
    else:
        # The content/evidence preflight already owns this staging directory.
        # Reusing it keeps probe, plans, theme and every render artifact inside
        # one transaction; a failed preflight can therefore never overwrite a
        # previous formal output.
        staging = prepared_dir
        run_id = prepared_run_id or staging.name
    render_args = argparse.Namespace(**vars(args))
    render_args.output_dir = staging
    for name in ("resume-plan.json", "typeset-plan.json", "data-probe.json"):
        source = args.output_dir / name
        if source.exists() and source.resolve() != (staging / name).resolve():
            shutil.copy2(source, staging / name)
    staged_theme = staging / "theme_vars.json"
    if theme_vars.resolve() != staged_theme.resolve():
        shutil.copy2(theme_vars, staged_theme)
    trace: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    source_hash: str | None = None
    theme_hash = sha256(staged_theme)
    frozen_inputs = {
        "profile_sha256": sha256(args.profile),
        "resume_plan_sha256": sha256(staging / "resume-plan.json"),
        "typeset_plan_sha256": sha256(staging / "typeset-plan.json"),
        "template_sha256": sha256(args.template),
        "renderer_sha256": sha256(Path(__file__).with_name("typst_renderer.py")),
        "theme_vars_sha256": theme_hash,
    }
    if args.jd_brief:
        frozen_inputs["jd_brief_sha256"] = sha256(args.jd_brief)
    if args.jd_evidence_map:
        frozen_inputs["jd_evidence_map_sha256"] = sha256(args.jd_evidence_map)
    final_status = "layout_gate_blocked"
    failure_code = "LAYOUT_GATE_BLOCKED"
    try:
        for round_number, state in enumerate(("normal", "compact_1", "compact_2")):
            layout_path = write_layout_vars(staging, state, round_number, previous,
                                            "initial_render" if state == "normal" else f"applied_{state}_spacing")
            # A failed pre-geometry gate must not leave an earlier round's
            # measurements looking like the current attempt.
            (staging / "geometry-qa.json").unlink(missing_ok=True)
            command = [
                sys.executable, str(Path(__file__).with_name("typst_renderer.py")),
                "--profile", str(args.profile), "--template", str(args.template),
                "--resume-plan", str(staging / "resume-plan.json"),
                "--typeset-plan", str(staging / "typeset-plan.json"),
                "--output-dir", str(staging), "--layout-vars", str(layout_path),
                "--theme-vars", str(staged_theme), "--internal-reflow",
            ]
            if args.inbox:
                command.extend(["--inbox", str(args.inbox)])
            if args.jd_brief:
                command.extend(["--jd-brief", str(args.jd_brief)])
            if args.jd_evidence_map:
                command.extend(["--jd-evidence-map", str(args.jd_evidence_map)])
            try:
                subprocess.run(command, check=True, text=True, capture_output=True)
            except subprocess.CalledProcessError as exc:
                error_code, detail = renderer_failure_details(exc)
                result = {"round": round_number, "layout_state": state,
                          "layout_vars_sha256": sha256(layout_path), **frozen_inputs,
                          "decision": "layout_gate_blocked", "error_code": error_code,
                          "renderer_exit_code": exc.returncode,
                          "reason": detail}
                trace.append(result)
                final_status = "layout_gate_blocked"
                failure_code = error_code
                break
            typst_source = staging / "resume.typ"
            current_hash = sha256(typst_source)
            if source_hash is None:
                source_hash = current_hash
            elif current_hash != source_hash:
                raise ValueError("REFLOW_SOURCE_MUTATION_ERROR: Typst source changed across layout states")
            if sha256(staged_theme) != theme_hash:
                raise ValueError("REFLOW_THEME_MUTATION_ERROR: theme_vars.json changed during reflow")
            hash_paths = {
                "profile_sha256": args.profile, "resume_plan_sha256": staging / "resume-plan.json",
                "typeset_plan_sha256": staging / "typeset-plan.json", "template_sha256": args.template,
                "renderer_sha256": Path(__file__).with_name("typst_renderer.py"),
                **({"jd_brief_sha256": args.jd_brief} if args.jd_brief else {}),
                **({"jd_evidence_map_sha256": args.jd_evidence_map} if args.jd_evidence_map else {}),
            }
            if any(sha256(path) != frozen_inputs[key] for key, path in hash_paths.items()):
                raise ValueError("REFLOW_INPUT_MUTATION_ERROR: a frozen non-layout input changed during reflow")
            geometry_path = staging / "geometry-qa.json"
            if not geometry_path.exists():
                raise ValueError("GEOMETRY_QA_ERROR: renderer did not write geometry QA output")
            result = parse_geometry(geometry_path)
            codes = {item.get("code") for item in result["findings"]}
            result.update({"round": round_number, "layout_state": state,
                           "layout_vars_sha256": sha256(layout_path), **frozen_inputs,
                           "typst_source_sha256": current_hash, "geometry_exit_code": 0,
                           "artifact_qa_exit_code": 0,
                           "artifact_sha256": sha256(staging / "resume.pdf")})
            # Collect every finding before routing.  PAGE_COUNT_ERROR is
            # recoverable only when the same render has no fatal physical or
            # provenance finding; otherwise compact reflow would hide the
            # actual defect and potentially publish a malformed candidate.
            fatal_codes = codes - {"PAGE_COUNT_ERROR", "BOTTOM_WHITESPACE_EXCESS"}
            if fatal_codes:
                result["decision"] = "layout_gate_blocked"
                result["fatal_findings"] = sorted(fatal_codes)
                trace.append(result)
                final_status = "layout_gate_blocked"
                failure_code = sorted(fatal_codes)[0]
                break
            if "PAGE_COUNT_ERROR" in codes:
                if state == "normal":
                    result["decision"] = "compact_1"; trace.append(result); previous = result; continue
                if state == "compact_1":
                    result["decision"] = "compact_2"; trace.append(result); previous = result; continue
                result["decision"] = "content_gate_blocked"; trace.append(result); final_status = "content_gate_blocked"; failure_code = "CONTENT_GATE_BLOCKED"; break
            if "BOTTOM_WHITESPACE_EXCESS" in codes:
                if state == "normal":
                    result["decision"] = "compact_1"; trace.append(result); previous = result; continue
                if state == "compact_1":
                    result["decision"] = "compact_2"; trace.append(result); previous = result; continue
                result["decision"] = "content_gate_blocked"; trace.append(result); final_status = "content_gate_blocked"; failure_code = "CONTENT_GATE_BLOCKED"; break
            if codes:
                result["decision"] = "layout_gate_blocked"; trace.append(result); final_status = "layout_gate_blocked"
                failure_code = sorted(codes)[0]
                break
            result["decision"] = "eligible_for_approval"; trace.append(result); final_status = "eligible_for_approval"; break
        trace_path = staging / "reflow-trace.json"
        last_result = trace[-1] if trace else {}
        is_sparse_page = (
            last_result.get("page_count") == 1
            and float(last_result.get("bottom_whitespace_pt") or 0) > MAX_BOTTOM_WHITESPACE_PT
        )
        is_overfull_page = last_result.get("page_count", 0) > 1
        # Overfull pages first get one evidence-only shorter-copy attempt.  If
        # that is still not enough, the next bounded action is removing one
        # whole lowest-ranked project.  Independent counters allow a
        # compression to hand off to pruning without permitting loops.
        if final_status == "content_gate_blocked" and is_overfull_page and compression_attempt < 1:
            compressed = _content_compress_candidate(staging)
            if compressed is not None:
                append_content_recovery_trace(staging, {
                    "status": "content_compression_applied",
                    "before_page_count": last_result.get("page_count"),
                    "content_mode": "compressed",
                    "rule": "authorized-Claim-only 30-40 CJK compression; no fact deletion",
                })
                atomic_write_json(staging / "typeset-plan-before-content-recovery.json",
                                  load_json(staging / "typeset-plan.json"))
                atomic_write_json(staging / "typeset-plan.json", compressed)
                return render_with_reflow(
                    args, template, theme_vars, prepared_dir=staging,
                    prepared_run_id=run_id, expansion_attempt=expansion_attempt,
                    compression_attempt=compression_attempt + 1,
                    prune_attempt=prune_attempt,
                )

        if final_status == "content_gate_blocked" and is_overfull_page and prune_attempt < 1:
            prune = _content_prune_candidate(staging)
            if prune is not None:
                candidate_typeset, candidate_plan, removed_id = prune
                append_content_recovery_trace(staging, {
                    "status": "project_prune_applied",
                    "before_page_count": last_result.get("page_count"),
                    "removed_project_id": removed_id,
                    "remaining_project_count": len(candidate_plan.projects),
                    "rule": "remove one whole lowest-ranked project; retain facts verbatim",
                })
                atomic_write_json(staging / "resume-plan-before-content-recovery.json",
                                  load_json(staging / "resume-plan.json"))
                atomic_write_json(staging / "typeset-plan-before-content-recovery.json",
                                  load_json(staging / "typeset-plan.json"))
                atomic_write_json(staging / "resume-plan.json", candidate_plan.model_dump(mode="json"))
                atomic_write_json(staging / "typeset-plan.json", candidate_typeset)
                prune_args = argparse.Namespace(**vars(args))
                # Keep the next admission check aligned with the pruned plan;
                # otherwise a recovery brief requesting four projects would be
                # mistaken for an Agent A selection mutation.
                if prune_args.jd_brief:
                    pruned_brief = load_json(prune_args.jd_brief)
                    pruned_brief["max_projects"] = len(candidate_plan.projects)
                    pruned_brief_path = staging / "jd-brief-content-pruned.json"
                    atomic_write_json(pruned_brief_path, pruned_brief)
                    prune_args.jd_brief = pruned_brief_path
                return render_with_reflow(
                    prune_args, template, theme_vars, prepared_dir=staging,
                    prepared_run_id=run_id, expansion_attempt=expansion_attempt,
                    compression_attempt=compression_attempt,
                    prune_attempt=prune_attempt + 1,
                )

        if final_status == "content_gate_blocked" and is_sparse_page and expansion_attempt < 1:
            recovery = _content_recovery_candidate(staging, args, template)
            if recovery is not None:
                candidate, candidate_plan, recovery_brief = recovery
                append_content_recovery_trace(staging, {
                    "status": "candidate_applied",
                    "before_bottom_whitespace_pt": last_result.get("bottom_whitespace_pt"),
                    "before_project_count": len(load_json(staging / "resume-plan.json").get("projects", [])),
                    "after_project_count": len(candidate_plan.projects),
                    "candidate_typeset_sha256": hashlib.sha256(
                        json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    "rule": "authorized-Claim-only recovery; no facts or font/layout changes",
                })
                atomic_write_json(staging / "typeset-plan-before-content-recovery.json",
                                  load_json(staging / "typeset-plan.json"))
                if recovery_brief is not None:
                    atomic_write_json(staging / "resume-plan-before-content-recovery.json",
                                      load_json(staging / "resume-plan.json"))
                    atomic_write_json(staging / "resume-plan.json", candidate_plan.model_dump(mode="json"))
                atomic_write_json(staging / "typeset-plan.json", candidate)
                recovery_args = argparse.Namespace(**vars(args))
                if recovery_brief is not None:
                    recovery_args.jd_brief = recovery_brief
                return render_with_reflow(
                    recovery_args, template, theme_vars, prepared_dir=staging,
                    prepared_run_id=run_id, expansion_attempt=expansion_attempt + 1,
                    compression_attempt=compression_attempt,
                    prune_attempt=prune_attempt,
                )
        atomic_write_json(trace_path, {"status": final_status, "run_id": run_id, "rounds": trace})
        if final_status == "eligible_for_approval":
            write_delivery_manifest(staging / "delivery-manifest.json", pdf_path=staging / "resume.pdf",
                                    manifest_path=staging / "project-manifest.json", typst_path=staging / "resume.typ",
                                    theme_path=staged_theme, geometry=trace[-1])
            # These are transaction-local inputs/diagnostics used to build or
            # retry Agent B.  They must never leak into the formal output
            # namespace, but leaving them in staging would make the otherwise
            # successful transaction fail at ``rmdir`` and could leave a
            # misleading delivery manifest behind.
            for transient_name in (
                "typeset-plan.generated.json", "agent-b-attempts.json",
                "typeset-plan-before-content-recovery.json",
                "resume-plan-before-content-recovery.json",
                "jd-brief-content-recovery.json", "jd-brief-content-pruned.json",
                "jd-evidence-map.generated.json",
            ):
                (staging / transient_name).unlink(missing_ok=True)
            names = ("data-probe.json", "resume-plan.json", "typeset-plan.json", "resume.pdf", "resume.typ", "project-manifest.json", "geometry-qa.json", "layout_vars.json",
                     "reflow-trace.json", "delivery-manifest.json", "theme_vars.json", "resume-photo.png")
            # A successful content recovery is part of the delivery audit:
            # retain the bounded expand/compress/prune action instead of
            # treating it as an unobservable temporary implementation detail.
            names = names + ("content-recovery-trace.json",)
            promote_render_transaction(staging, args.output_dir, names=names)
            staging.rmdir()
            trace_path = args.output_dir / "reflow-trace.json"
        else:
            quarantine = quarantine_render_transaction(
                args.output_dir, run_id, staging, code=failure_code,
                detail="render transaction did not satisfy the final PDF gate",
                phase="pdf_reflow", inputs=frozen_inputs,
            )
            trace_path = quarantine / "reflow-trace.json"
        if final_status == "content_gate_blocked":
            event_path = trace_path.parent / "skillopt-event.json"
            recovery_request = None
            if event_path.is_file():
                try:
                    event_payload = load_json(event_path)
                    recovery_request = event_payload.get("auto_skillopt", {}).get("recovery_request")
                except (OSError, ValueError, json.JSONDecodeError):
                    recovery_request = None
            recovery_trace = trace_path.parent / "content-recovery-trace.json"
            if recovery_trace.is_file():
                reason = (
                    "automatic evidence-only content recovery was attempted, but the result still "
                    "does not satisfy the one-page density bound; authorize another relevant project/Claim "
                    "or approve a wider content budget"
                )
            elif is_sparse_page:
                reason = (
                    "page is one A4 page but content is too sparse; an authorized Claim/project recovery "
                    "candidate was not safely applicable"
                )
            else:
                reason = "content is too long for a safe one-page A4 result after compact_2"
            print(json.dumps({"status": "needs_user_input", "gate": final_status,
                              "reason": reason,
                              "reflow_trace": str(trace_path),
                              "skillopt_event": str(event_path) if event_path.is_file() else None,
                              "content_recovery_trace": str(recovery_trace) if recovery_trace.is_file() else None,
                              "content_recovery_request": recovery_request}, ensure_ascii=False))
        return {"status": final_status, "trace_path": trace_path}
    except Exception as exc:
        quarantine = quarantine_render_transaction(
            args.output_dir, run_id, staging, code=getattr(exc, "code", "RENDER_TRANSACTION_ERROR"),
            detail=str(exc), phase="pdf_reflow", inputs=frozen_inputs,
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, help="Git-ignored ingestion_inbox.yaml used to verify approved work highlights")
    parser.add_argument("--jd-brief", type=Path, help="Git-ignored structured JD requirements for source-backed project selection")
    parser.add_argument("--jd-evidence-map", type=Path, help="Read-only local-project evidence map paired with --jd-brief")
    parser.add_argument(
        "--project-dir", action="append",
        help="Convenience JD input: PROJECT_ID=/absolute/or/local/path (repeat for each local project; scanned read-only)",
    )
    parser.add_argument("--agent-a-output", type=Path, help="Validated Agent A JSON; omit for deterministic source-preserving selection")
    parser.add_argument("--agent-b-output", type=Path, help="Initial Agent B JSON before rendering")
    parser.add_argument("--agent-b-retry-output", type=Path, nargs="*", default=[], help="At most two same-Claim contract rewrites")
    parser.add_argument("--render", action="store_true", help="Invoke Typst renderer after validating Agent B output")
    parser.add_argument("--docx", action="store_true", help="Also create an editable DOCX; it is not used for PDF layout decisions")
    parser.add_argument("--theme-variant", "--design-variant", dest="theme_variant", choices=tuple(DESIGN_VARIANTS), help="Explicitly approved executive-editorial theme for rendering")
    args = parser.parse_args()
    preflight_run_id: str | None = None
    preflight_dir: Path | None = None
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if args.render:
            preflight_run_id, preflight_dir = begin_render_transaction(args.output_dir)
        work_dir = preflight_dir or args.output_dir
        if args.docx and not args.render:
            raise ValueError("DOCX_DELIVERY_ORDER_ERROR: --docx requires --render and an eligible PDF")
        profile = Profile.model_validate(load_yaml(args.profile))
        template = Template.model_validate(load_yaml(args.template))
        if profile.identity.market != template.market:
            raise ValueError("profile and template market routes differ")
        _prepare_jd_evidence_map(args, work_dir)
        selection = resolve_project_selection(
            profile=profile, template=template, jd_brief_path=args.jd_brief,
            jd_evidence_map_path=args.jd_evidence_map,
        )
        validate_employment_provenance(profile, args.inbox)
        probe = data_probe(profile, template, selection)
        write_json(work_dir / "data-probe.json", probe)
        if any(item.status in {"needs_user_input", "evidence_gate_blocked"} for item in probe):
            status = "evidence_gate_blocked" if any(item.status == "evidence_gate_blocked" for item in probe) else "needs_user_input"
            quarantine = quarantine_build_failure(
                args.output_dir, preflight_run_id, preflight_dir,
                code=status.upper(), detail="data probe did not satisfy the evidence gate",
                phase="content_probe",
            )
            preflight_dir = None
            print(json.dumps({"status": status, "probe": [item.model_dump() for item in probe],
                              "quarantine": str(quarantine)}, ensure_ascii=False))
            return 3
        agent_a = (ResumePlan.model_validate(load_json(args.agent_a_output))
                   if args.agent_a_output else default_agent_a(profile, template, selection))
        validate_agent_a(agent_a, profile, template, selection)
        # Legacy Agent A JSON did not carry its resolved selection.  The
        # pipeline, not the model, attaches the freshly verified scope before
        # it becomes a renderer input.
        if agent_a.selection is None:
            agent_a = agent_a.model_copy(update={"selection": selection})
        write_json(work_dir / "resume-plan.json", agent_a)
        if not args.agent_b_output:
            # A JD-driven invocation is expected to produce a resume, not
            # stop after Agent A.  Generate a deterministic first Agent B
            # candidate from the already validated plan and approved inbox.
            # The candidate still goes through the exact same admission gate
            # and may be rewritten by the bounded content-recovery loop.
            try:
                from skillopt_auto_loop import build_typeset_candidate

                generated = build_typeset_candidate(agent_a.model_dump(mode="json"), None, content_mode="normal")
                if generated is None:
                    raise NeedsUserInputError(
                        "NEEDS_USER_INPUT: authorized Claims cannot form the initial project stage copy"
                    )
                generated["employment"] = generate_employment_typeset(profile, args.inbox)
                generated_path = work_dir / "typeset-plan.generated.json"
                atomic_write_json(generated_path, generated)
                agent_b = validate_agent_b_attempts(
                    [generated_path], agent_a, profile, args.inbox, work_dir,
                )
            except (RetryableContractError, EvidenceGateError, NeedsUserInputError):
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise NeedsUserInputError(
                    f"NEEDS_USER_INPUT: automatic Agent B generation failed safely: {exc}"
                ) from exc
        else:
            agent_b = validate_agent_b_attempts(
                [args.agent_b_output, *args.agent_b_retry_output], agent_a, profile, args.inbox, work_dir,
            )
        write_json(work_dir / "typeset-plan.json", agent_b)
        theme_vars: Path | None = None
        if args.render or args.docx:
            if not args.theme_variant:
                review_path = args.output_dir / "design-review.json"
                review_path.write_text(json.dumps(theme_review_payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if preflight_dir is not None and preflight_dir.exists():
                    quarantine_render_transaction(
                        args.output_dir, preflight_run_id or preflight_dir.name, preflight_dir,
                        code="THEME_REVIEW_PENDING", detail="approved theme variant is required before rendering",
                        phase="theme_review",
                    )
                    preflight_dir = None
                print(json.dumps({"status": "theme_review_pending", "design_review": str(review_path)}, ensure_ascii=False))
                return 4
            theme_vars = work_dir / "theme_vars.json"
            theme_vars.write_text(json.dumps(theme_payload(args.theme_variant), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        render_result = {"status": "skipped"}
        if args.render:
            assert theme_vars is not None
            render_result = render_with_reflow(args, template, theme_vars, prepared_dir=preflight_dir, prepared_run_id=preflight_run_id)
            # The render transaction either promoted or quarantined this
            # directory.  Do not attempt to quarantine it again in the outer
            # exception handlers.
            preflight_dir = None
            theme_vars = args.output_dir / "theme_vars.json"
        # DOCX is a delivery consistency check and only runs after PDF eligibility.
        if args.docx and args.render and render_result["status"] == "eligible_for_approval":
            assert theme_vars is not None
            # A new DOCX attempt must not inherit an older authorization
            # record.  The delivery manifest is recreated only after the new
            # bytes pass the DOCX gate.
            (args.output_dir / "docx-delivery-manifest.json").unlink(missing_ok=True)
            command = [sys.executable, str(Path(__file__).with_name("docx_renderer.py")), "--profile", str(args.profile), "--template", str(args.template), "--resume-plan", str(args.output_dir / "resume-plan.json"), "--typeset-plan", str(args.output_dir / "typeset-plan.json"), "--theme-vars", str(theme_vars), "--output", str(args.output_dir / "resume.docx"), "--internal-delivery"]
            try:
                subprocess.run(command, check=True)
            except subprocess.CalledProcessError as exc:
                # PDF has already passed its authoritative gate.  A DOCX
                # renderer failure is a delivery-only failure: quarantine the
                # editable artifacts, emit the common SkillOpt event, and do
                # not roll back or reflow the valid PDF.
                docx_quarantine = quarantine_artifacts(
                    args.output_dir,
                    (args.output_dir / "resume.docx", args.output_dir / "docx-project-manifest.json",
                     args.output_dir / ".docx-qa" / "resume.pdf"),
                    code="DOCX_DELIVERY_BLOCKED",
                    detail=f"DOCX renderer exited with status {exc.returncode}: {exc}",
                    phase="docx_delivery",
                )
                print(json.dumps({
                    "status": "delivery_gate_blocked",
                    "reason": "DOCX delivery failed; authoritative PDF remains eligible",
                    "pdf": str(args.output_dir / "resume.pdf"),
                    "docx_quarantine": str(docx_quarantine),
                }, ensure_ascii=False))
                return 3
        if args.render and args.docx and render_result["status"] == "eligible_for_approval":
            try:
                docx_manifest = args.output_dir / "docx-project-manifest.json"
                docx_gate = run_docx_delivery_gate(
                    docx_path=args.output_dir / "resume.docx",
                    manifest_path=docx_manifest if docx_manifest.is_file() else args.output_dir / "project-manifest.json",
                    theme_path=args.output_dir / "theme_vars.json",
                    profile_path=args.profile, market=template.market,
                    qa_dir=args.output_dir / ".docx-qa",
                    renderer_path=Path(__file__).with_name("docx_renderer.py"),
                )
            except ResumeQAError as exc:
                (args.output_dir / "docx-delivery-manifest.json").unlink(missing_ok=True)
                quarantine_artifacts(
                    args.output_dir, (args.output_dir / "resume.docx", args.output_dir / "docx-project-manifest.json"),
                    code=exc.code, detail=exc.detail, phase="docx_delivery",
                )
                raise
            atomic_write_json(args.output_dir / "docx-delivery-manifest.json", {
                "status": "eligible_for_approval",
                "docx": "resume.docx",
                "project_manifest": "project-manifest.json",
                "theme_vars": "theme_vars.json",
                "sha256": {
                    "docx": sha256(args.output_dir / "resume.docx"),
                    "project_manifest": sha256(args.output_dir / "project-manifest.json"),
                    "docx_project_manifest": sha256(docx_manifest),
                    "theme_vars": sha256(args.output_dir / "theme_vars.json"),
                },
                "qa": docx_gate,
            })
            check_delivery_manifest(args.output_dir / "docx-delivery-manifest.json", expected_paths={
                "docx": args.output_dir / "resume.docx",
                "project_manifest": args.output_dir / "project-manifest.json",
                "theme_vars": args.output_dir / "theme_vars.json",
            })
            reflow_trace = args.output_dir / "reflow-trace.json"
            if reflow_trace.is_file():
                trace_payload = load_json(reflow_trace)
                trace_payload["docx_qa"] = docx_gate
                atomic_write_json(reflow_trace, trace_payload)
        status = render_result["status"] if args.render else "ready"
        print(json.dumps({"status": status, "output_dir": str(args.output_dir)}, ensure_ascii=False))
        return 0 if status in {"ready", "eligible_for_approval"} else 3
    except ResumeQAError as exc:
        quarantine = quarantine_build_failure(
            args.output_dir, preflight_run_id, preflight_dir,
            code=exc.code, detail=exc.detail, phase="build",
        )
        preflight_dir = None
        print(json.dumps({"status": "delivery_gate_blocked", "reason": str(exc),
                          "quarantine": str(quarantine)}, ensure_ascii=False))
        return 3
    except NeedsUserInputError as exc:
        quarantine = quarantine_build_failure(
            args.output_dir, preflight_run_id, preflight_dir,
            code="NEEDS_USER_INPUT", detail=str(exc), phase="content_gate",
        )
        preflight_dir = None
        print(json.dumps({"status": "needs_user_input", "reason": str(exc),
                          "quarantine": str(quarantine)}, ensure_ascii=False))
        return 3
    except EvidenceGateError as exc:
        quarantine = quarantine_build_failure(
            args.output_dir, preflight_run_id, preflight_dir,
            code="EVIDENCE_GATE_BLOCKED", detail=str(exc), phase="evidence_gate",
        )
        preflight_dir = None
        print(json.dumps({"status": "evidence_gate_blocked", "reason": str(exc),
                          "quarantine": str(quarantine)}, ensure_ascii=False))
        return 3
    except (OSError, ValueError, ValidationError, subprocess.CalledProcessError) as exc:
        quarantine = quarantine_build_failure(
            args.output_dir, preflight_run_id, preflight_dir,
            code="BUILD_ERROR", detail=str(exc), phase="build",
        )
        preflight_dir = None
        print(json.dumps({"status": "build_error", "reason": str(exc),
                          "quarantine": str(quarantine)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
