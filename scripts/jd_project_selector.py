#!/usr/bin/env python3
"""Create and verify a JD-to-local-project relevance map without adding facts.

The scanner is deliberately lexical and read-only: it never executes project
code, follows symlinks outside a user-supplied root, edits ``private.yaml``,
or treats a keyword match as resume content.  Its output only decides which
already-authorized projects Agent A may consider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Iterable

from build_resume import (
    JDBrief,
    JDEvidenceMap,
    Profile,
    Template,
    load_json,
    load_yaml,
    resolve_project_selection,
)


TEXT_SUFFIXES = frozenset({
    ".md", ".txt", ".rst", ".py", ".js", ".jsx", ".ts", ".tsx", ".java",
    ".go", ".rs", ".sql", ".json", ".yaml", ".yml", ".toml", ".ini",
})
IGNORED_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"})
MAX_FILE_BYTES = 1_000_000
MAX_FILES_PER_PROJECT = 1_000


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_project_dir(value: str) -> tuple[str, Path]:
    project_id, separator, raw_path = value.partition("=")
    if not separator or not project_id.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("--project-dir must be PROJECT_ID=/absolute/or/local/path")
    root = Path(raw_path).expanduser().resolve()
    if not root.is_dir():
        raise argparse.ArgumentTypeError(f"project root is not a directory: {root}")
    return project_id.strip(), root


def local_text_files(root: Path) -> Iterable[Path]:
    count = 0
    for candidate in root.rglob("*"):
        if any(part in IGNORED_DIRS for part in candidate.relative_to(root).parts):
            continue
        if candidate.is_symlink() or not candidate.is_file() or candidate.suffix.lower() not in TEXT_SUFFIXES:
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.stat().st_size > MAX_FILE_BYTES:
            continue
        count += 1
        if count > MAX_FILES_PER_PROJECT:
            raise ValueError(f"JD_SOURCE_SCAN_LIMIT: more than {MAX_FILES_PER_PROJECT} eligible files under {root}")
        yield resolved


def scan(brief: JDBrief, project_dirs: list[tuple[str, Path]]) -> JDEvidenceMap:
    matches: list[dict[str, object]] = []
    for project_id, root in project_dirs:
        for path in local_text_files(root):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for requirement in brief.requirements:
                for line_number, line in enumerate(lines, 1):
                    matched = [keyword for keyword in requirement.keywords if keyword and keyword in line]
                    if not matched:
                        continue
                    window_start, window_end = max(0, line_number - 2), min(len(lines), line_number + 1)
                    excerpt = "\n".join(lines[window_start:window_end])
                    matches.append({
                        "project_id": project_id,
                        "requirement_id": requirement.id,
                        "path": str(path),
                        "line_start": window_start + 1,
                        "line_end": window_end,
                        "source_sha256": sha256(path),
                        "excerpt": excerpt,
                        "matched_keywords": matched,
                    })
                    break
    if not matches:
        raise ValueError("NEEDS_USER_INPUT: no JD requirement keyword has a local-project source match")
    return JDEvidenceMap(schema_version="1.0", jd_text_sha256=brief.jd_text_sha256, matches=matches)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="Read only supplied project roots and write JD relevance candidates")
    scan_parser.add_argument("--jd-brief", type=Path, required=True)
    scan_parser.add_argument("--project-dir", action="append", type=parse_project_dir, required=True)
    scan_parser.add_argument("--output", type=Path, required=True)
    select_parser = subparsers.add_parser("select", help="Validate a map and write the deterministic selected project IDs")
    select_parser.add_argument("--profile", type=Path, required=True)
    select_parser.add_argument("--template", type=Path, required=True)
    select_parser.add_argument("--jd-brief", type=Path, required=True)
    select_parser.add_argument("--jd-evidence-map", type=Path, required=True)
    select_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "scan":
            brief = JDBrief.model_validate(load_json(args.jd_brief))
            write_json(args.output, scan(brief, args.project_dir))
            print(json.dumps({"status": "jd_evidence_map_ready", "output": str(args.output)}, ensure_ascii=False))
            return 0
        profile = Profile.model_validate(load_yaml(args.profile))
        template = Template.model_validate(load_yaml(args.template))
        selection = resolve_project_selection(
            profile=profile, template=template, jd_brief_path=args.jd_brief,
            jd_evidence_map_path=args.jd_evidence_map,
        )
        write_json(args.output, selection)
        print(json.dumps({"status": "jd_project_selection_ready", "output": str(args.output)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "needs_user_input", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
