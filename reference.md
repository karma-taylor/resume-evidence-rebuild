# SkillOpt architecture

## Goal

SkillOpt improves the instructions that govern one-page A4 resume layout while preserving the evidence-first contract of this skill. It optimizes instructions, not candidates' facts and not a live document in place.

## Three-stage loop

1. **Forward execution / rollout.** A normal resume run produces a DOCX/PDF and rendered page images. When the A4 QA fails, the runner saves a redacted trajectory: input manifest, incumbent skill snapshot/hash, generated-artifact manifest, and structured error findings. This is a negative sample, not a training example to publish.
2. **Backward reflection.** SkillOpt groups recent failures by error code and asks an optimizer model for a bounded JSON Patch. The model sees the evidence and only the permitted text sections. It cannot edit frontmatter, privacy/evidence policies, benchmark fixtures, or user files.
3. **Validation gate.** The patched candidate runs against the frozen offline benchmark. The candidate may replace the incumbent only when its complete-set A4 QA pass rate is strictly higher. This makes the optimization loop hill-climbing with a hard safety boundary rather than autonomous rewriting.

## Separation of concerns

| Plane | Responsibility | May mutate `SKILL.md`? |
| --- | --- | --- |
| Rollout | Execute the existing skill and record failures | No |
| Optimizer | Diagnose aggregate failure evidence and propose a patch | Candidate only |
| Gate | Run benchmark, compare scores, retain evidence | Only after strict improvement |
| Human maintainer | Review rejected proposals or expand allowed rules | Yes, deliberately |

## Why bounded edits

The most common layout regressions are local: excessive footer whitespace, overly dense bullet lines, oversized contact blocks, clipping, or CJK glyph fallback. A small, reviewable patch to layout wording is easier to attribute and roll back than a full prompt rewrite. The gate is intentionally indifferent to prose quality unless the predefined A4 QA confirms an improvement.

## Storage model

Keep runtime data outside the published skill source tree, for example:

```text
<runtime-root>/
├── rollouts/YYYY-MM/*.jsonl       # redacted failure trajectories
│   └── <run-id>/inputs/            # access-restricted original input snapshots
├── benchmarks/                    # 50 frozen, locally held fixtures
├── candidates/<run-id>/SKILL.md
├── evaluations/<run-id>.json
└── rejected/<run-id>.json
```

The repository may retain only schemas, synthetic examples, and code. The `assets/` folder documents the expected local fixture layout; it does not contain real materials.
