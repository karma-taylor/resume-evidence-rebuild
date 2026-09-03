"""Strict, allow-listed executive-editorial design tokens for resume renderers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
DESIGN_VARIANTS: dict[str, dict[str, Any]] = {
    "executive_editorial_a": {
        "description": "深海军蓝标题锚点与最克制的细分隔线。",
        "tokens": {
            "palette": {"ink": "#1D2630", "muted": "#667085", "accent": "#1E3A5F", "rule": "#6B91B8"},
            "lines": {"header_rule_pt": 0.9, "section_rule_pt": 0.45, "section_marker_width_pt": 2.5, "section_marker_height_pt": 11.0},
            "hierarchy": {"date_color": "#667085", "overview_color": "#667085", "title_weight": "bold"},
        },
    },
    "executive_editorial_b": {
        "description": "钢蓝标题标记，强化日期与项目层级。",
        "tokens": {
            "palette": {"ink": "#202833", "muted": "#52677D", "accent": "#285A85", "rule": "#7FA6C7"},
            "lines": {"header_rule_pt": 1.0, "section_rule_pt": 0.55, "section_marker_width_pt": 3.0, "section_marker_height_pt": 11.0},
            "hierarchy": {"date_color": "#52677D", "overview_color": "#52677D", "title_weight": "bold"},
        },
    },
    "executive_editorial_c": {
        "description": "深灰主体与银蓝细节，最接近传统高管简历。",
        "tokens": {
            "palette": {"ink": "#25282D", "muted": "#6B7280", "accent": "#38566F", "rule": "#A6B8C5"},
            "lines": {"header_rule_pt": 0.85, "section_rule_pt": 0.4, "section_marker_width_pt": 2.0, "section_marker_height_pt": 10.0},
            "hierarchy": {"date_color": "#6B7280", "overview_color": "#6B7280", "title_weight": "bold"},
        },
    },
}


def theme_review_payload() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "style": "executive_editorial",
        "candidates": [
            {"variant_id": variant_id, **variant}
            for variant_id, variant in DESIGN_VARIANTS.items()
        ],
    }


def theme_payload(variant_id: str) -> dict[str, Any]:
    if variant_id not in DESIGN_VARIANTS:
        raise ValueError(f"DESIGN_TOKEN_ERROR: unsupported variant {variant_id!r}")
    return {
        "schema_version": SCHEMA_VERSION,
        "variant_id": variant_id,
        "tokens": DESIGN_VARIANTS[variant_id]["tokens"],
    }


def load_theme(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"DESIGN_TOKEN_ERROR: cannot read approved design tokens: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("DESIGN_TOKEN_ERROR: approved design must be an object")
    expected = theme_payload(str(payload.get("variant_id", "")))
    if payload != expected:
        raise ValueError("DESIGN_TOKEN_ERROR: token payload differs from an approved executive-editorial variant")
    return payload


# Transitional aliases keep old local commands readable while all new pipeline
# interfaces use theme_vars.json. They do not broaden the allow-list.
design_review_payload = theme_review_payload
approved_payload = theme_payload
load_approved = load_theme
