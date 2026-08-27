#!/usr/bin/env python3
"""Stage an accepted SkillOpt candidate on a review branch; never writes main."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(*command: str, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--branch", required=True, help="Must use a review/ prefix")
    parser.add_argument("--publish", action="store_true", help="Push branch and open a GitHub PR with gh")
    args = parser.parse_args()
    if not args.branch.startswith("review/"):
        parser.error("--branch must start with review/")
    if not args.candidate.is_file() or not args.report.is_file():
        parser.error("candidate and report must exist")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if report.get("decision") != "accepted":
        parser.error("only accepted candidate reports may create a review branch")
    run("git", "switch", "main", cwd=args.repo)
    run("git", "switch", "-c", args.branch, cwd=args.repo)
    (args.repo / "SKILL.md").write_text(args.candidate.read_text(encoding="utf-8"), encoding="utf-8")
    report_target = args.repo / "pr-reports" / f"{args.branch.replace('/', '-')}.json"
    report_target.parent.mkdir(exist_ok=True)
    report_target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run("git", "add", "SKILL.md", str(report_target.relative_to(args.repo)), cwd=args.repo)
    run("git", "commit", "-m", "skillopt: candidate layout rule update", cwd=args.repo)
    if args.publish:
        run("git", "push", "-u", "origin", args.branch, cwd=args.repo)
        run("gh", "pr", "create", "--base", "main", "--head", args.branch, "--fill", cwd=args.repo)
    print(args.branch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
