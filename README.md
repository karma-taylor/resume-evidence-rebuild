# Resume Evidence Rebuild

An evidence-first Codex Skill for repeatable one-page A4 resume generation. The public repository contains reusable rules, schemas, templates, deterministic QA, and synthetic examples only.

Use a local, Git-ignored `profiles/private.yaml` for any real candidate identity, contact information, photo, employment, education, project evidence, or metrics. See `examples/sample.profile.yaml` for a fictional shape.

The rendering path is `profile + template → Data Probe → Agent A → Agent B → Typst PDF → geometry QA`. SkillOpt proposals create reviewable candidate branches/PRs only after private benchmark validation.
