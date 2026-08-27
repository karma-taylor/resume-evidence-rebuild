#!/usr/bin/env python3
"""Evidence-first resume pipeline: profile + template -> plans -> Typst PDF."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator


class Claim(BaseModel):
    id: str
    text: str
    source: str
    scope: str
    confidence: Literal["verified", "bounded"]
    allowed_for_resume: bool
    kind: Literal["context", "architecture", "control", "metric", "delivery"] = "context"


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


class Profile(BaseModel):
    identity: Identity
    education: list[dict]
    employment: list[dict]
    certifications: list[str] = Field(default_factory=list)
    projects: list[Project]


class Template(BaseModel):
    id: str
    target_role: str
    market: Literal["CN", "NA", "FOREIGN"]
    project_ids: list[str] = Field(min_length=3, max_length=4)
    layout: dict


class ProbeProject(BaseModel):
    id: str
    status: Literal["ready", "bounded", "needs_user_input"]
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


class TypesetBullet(BaseModel):
    text: str
    bold_phrases_used: list[str] = Field(min_length=1, max_length=2)
    source_claim_ids: list[str] = Field(min_length=1)

    @field_validator("bold_phrases_used")
    @classmethod
    def no_empty_phrases(cls, value: list[str]) -> list[str]:
        if not all(item.strip() for item in value):
            raise ValueError("bold_phrases_used cannot contain empty strings")
        return value


class TypesetProject(BaseModel):
    id: str
    bullets: list[TypesetBullet] = Field(min_length=3, max_length=4)


class TypesetPlan(BaseModel):
    projects: list[TypesetProject] = Field(min_length=3, max_length=4)


def cjk_count(text: str) -> int:
    return sum("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff" for char in text)


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


def data_probe(profile: Profile, template: Template) -> list[ProbeProject]:
    by_id = {project.id: project for project in profile.projects}
    results: list[ProbeProject] = []
    for project_id in template.project_ids:
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
        if questions:
            results.append(ProbeProject(id=project.id, status="needs_user_input", questions=questions))
        elif any(claim.kind == "metric" and claim.confidence == "verified" for claim in allowed):
            results.append(ProbeProject(id=project.id, status="ready"))
        else:
            results.append(ProbeProject(id=project.id, status="bounded", questions=["No authorized numeric metric; use delivery, control, validation, or scope wording only."]))
    return results


def default_agent_a(profile: Profile, template: Template) -> ResumePlan:
    by_id = {project.id: project for project in profile.projects}
    projects = []
    for project_id in template.project_ids:
        project = by_id[project_id]
        claims = [claim for claim in project.claims if claim.allowed_for_resume]
        projects.append(ResumePlanProject(id=project.id, title=project.title, start=project.start, end=project.end, tags=project.tags, claim_ids=[claim.id for claim in claims], claims=claims))
    return ResumePlan(target_role=template.target_role, projects=projects)


def validate_agent_a(plan: ResumePlan, profile: Profile, template: Template) -> None:
    allowed_projects = set(template.project_ids)
    private_projects = {project.id: project for project in profile.projects}
    if {project.id for project in plan.projects} != allowed_projects:
        raise ValueError("Agent A must select exactly the template's authorized project IDs")
    for project in plan.projects:
        private = private_projects[project.id]
        private_claims = {claim.id: claim for claim in private.claims if claim.allowed_for_resume}
        if not set(project.claim_ids).issubset(private_claims):
            raise ValueError(f"Agent A selected an unauthorized claim for {project.id}")
        for claim in project.claims:
            original = private_claims.get(claim.id)
            if original is None or claim != original:
                raise ValueError(f"Agent A altered claim {claim.id}; claims must remain source-identical")


def validate_agent_b(copy: TypesetPlan, plan: ResumePlan) -> None:
    plan_projects = {project.id: project for project in plan.projects}
    if {project.id for project in copy.projects} != set(plan_projects):
        raise ValueError("Agent B project IDs must exactly match Agent A")
    for project in copy.projects:
        claims = {claim.id: claim for claim in plan_projects[project.id].claims}
        for bullet in project.bullets:
            count = cjk_count(bullet.text)
            if not 60 <= count <= 70:
                raise ValueError(f"BULLET_LENGTH_ERROR: {project.id} bullet has {count} CJK characters; expected 60-70")
            if not set(bullet.source_claim_ids).issubset(claims):
                raise ValueError(f"Agent B references an unauthorized claim in {project.id}")
            source_text = "\n".join(claims[claim_id].text for claim_id in bullet.source_claim_ids)
            for phrase in bullet.bold_phrases_used:
                if phrase not in bullet.text:
                    raise ValueError(f"BULLET_BOLD_MISSING_ERROR: {phrase!r} is absent from bullet text")
                if phrase not in source_text:
                    raise ValueError(f"BULLET_BOLD_MISSING_ERROR: {phrase!r} is not grounded in source_claim_ids")


def write_json(path: Path, payload: BaseModel | list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, list):
        data = [item.model_dump(mode="json") for item in payload]
    else:
        data = payload.model_dump(mode="json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--agent-a-output", type=Path, help="Validated Agent A JSON; omit for deterministic source-preserving selection")
    parser.add_argument("--agent-b-output", type=Path, help="Required validated Agent B JSON before rendering")
    parser.add_argument("--render", action="store_true", help="Invoke Typst renderer after validating Agent B output")
    parser.add_argument("--docx", action="store_true", help="Also create an editable DOCX; it is not used for PDF layout decisions")
    args = parser.parse_args()
    try:
        profile = Profile.model_validate(load_yaml(args.profile))
        template = Template.model_validate(load_yaml(args.template))
        if profile.identity.market != template.market:
            raise ValueError("profile and template market routes differ")
        probe = data_probe(profile, template)
        write_json(args.output_dir / "data-probe.json", probe)
        if any(item.status == "needs_user_input" for item in probe):
            print(json.dumps({"status": "needs_user_input", "probe": [item.model_dump() for item in probe]}, ensure_ascii=False))
            return 3
        agent_a = ResumePlan.model_validate(load_json(args.agent_a_output)) if args.agent_a_output else default_agent_a(profile, template)
        validate_agent_a(agent_a, profile, template)
        write_json(args.output_dir / "resume-plan.json", agent_a)
        if not args.agent_b_output:
            print(json.dumps({"status": "agent_b_required", "resume_plan": str(args.output_dir / "resume-plan.json")}, ensure_ascii=False))
            return 0
        agent_b = TypesetPlan.model_validate(load_json(args.agent_b_output))
        validate_agent_b(agent_b, agent_a)
        write_json(args.output_dir / "typeset-plan.json", agent_b)
        if args.render:
            command = [sys.executable, str(Path(__file__).with_name("typst_renderer.py")), "--profile", str(args.profile), "--template", str(args.template), "--resume-plan", str(args.output_dir / "resume-plan.json"), "--typeset-plan", str(args.output_dir / "typeset-plan.json"), "--output-dir", str(args.output_dir)]
            subprocess.run(command, check=True)
        if args.docx:
            command = [sys.executable, str(Path(__file__).with_name("docx_renderer.py")), "--profile", str(args.profile), "--template", str(args.template), "--resume-plan", str(args.output_dir / "resume-plan.json"), "--typeset-plan", str(args.output_dir / "typeset-plan.json"), "--output", str(args.output_dir / "resume.docx")]
            subprocess.run(command, check=True)
        print(json.dumps({"status": "ready", "output_dir": str(args.output_dir)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, ValidationError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
