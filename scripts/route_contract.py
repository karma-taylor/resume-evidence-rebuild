"""Shared route and result-policy contracts for the resume pipeline."""
from __future__ import annotations

import re
from typing import Any


CANONICAL_ROUTES = frozenset({
    "eligible_for_approval",
    "bounded",
    "needs_user_input",
    "blocked",
})

ROUTE_ALIASES = {
    "ready": "eligible_for_approval",
    "evidence_gate_blocked": "blocked",
    "content_gate_blocked": "needs_user_input",
    "layout_gate_blocked": "blocked",
    "delivery_gate_blocked": "blocked",
    "failed": "blocked",
    "timeout": "blocked",
}


def normalize_route(value: Any) -> str:
    """Normalize legacy benchmark names while preserving unknown values."""
    route = str(value or "").strip()
    return ROUTE_ALIASES.get(route, route)


def route_matches(expected: Any, actual: Any, *, artifacts_match: bool = False) -> bool:
    """Compare policy routes with final delivery status.

    ``bounded`` is an evidence policy route. A bounded resume may still have
    a final delivery manifest whose status is ``eligible_for_approval``.
    """
    expected_route = normalize_route(expected)
    actual_route = normalize_route(actual)
    if expected_route == actual_route:
        return True
    return expected_route == "bounded" and actual_route == "eligible_for_approval" and artifacts_match


# Recovery can take a source-verbatim suffix and therefore legitimately drop
# the first CJK character from a redacted fixture identifier. Treat those
# clipped synthetic IDs as identifiers too; they are not business metrics.
IDENTIFIER_RE = re.compile(
    r"(?:匿名项目|名项目|项目|project|fixture)[-_－][0-9０-９]+[-_－][0-9０-９]+",
    re.IGNORECASE,
)


def metric_tokens_for_text(text: str, *, numeric_re: Any) -> list[str]:
    """Find metric tokens while ignoring synthetic fixture/project IDs."""
    scrubbed = IDENTIFIER_RE.sub("", text)
    return list(numeric_re.findall(scrubbed))


def result_kinds_for_text(text: str, *, numeric_re: Any, effect_terms: tuple[str, ...]) -> set[str]:
    """Return permissible Claim kinds for a terminal result."""
    has_metric = bool(metric_tokens_for_text(text, numeric_re=numeric_re) or any(term in text for term in effect_terms))
    return {"metric"} if has_metric else {"architecture", "control", "delivery"}
