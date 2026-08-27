# Resume Evidence Rebuild

An evidence-first Codex Skill for repeatable one-page A4 resume generation. The public repository contains reusable rules, schemas, templates, deterministic QA, and synthetic examples only.

The normal community-user flow is **zero configuration**: upload an existing resume and any authorized project materials/JD in the Codex conversation. The Skill extracts evidence and builds a local, Git-ignored `profiles/private.yaml`; the user does not need to hand-write YAML. The profile is the local source of truth for real identity, contact information, photo, employment, education, project evidence, and metrics. Advanced users may maintain it directly. See `examples/sample.profile.yaml` for a fictional shape.

The rendering path is `authorized materials → private profile → template → Data Probe → Agent A → Agent B → Typst PDF → geometry QA`. SkillOpt proposals create reviewable candidate branches/PRs only after private benchmark validation.
