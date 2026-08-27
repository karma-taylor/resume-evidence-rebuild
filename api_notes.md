# Optimizer API and local text-space contract

## API input and output

`scripts/skillopt_pipeline.py` calls an OpenAI-compatible API only when `--execute` is set. Runtime configuration uses `SKILLOPT_API_KEY`, `SKILLOPT_MODEL` and optional `SKILLOPT_BASE_URL`; credentials never enter source control, logs or fixtures.

The request consists of the SkillOpt system prompt, redacted failure summaries and the three mutable public instruction sections. The response must be JSON matching `OptimizerResponse`: `summary`, `hypothesis`, `patch`, and `expected_effect`. Patches are RFC 6902 operations limited to three operations and the allowed layout sections.

## Local text space and promotion

The incumbent `SKILL.md` is read-only during reflection. A candidate is written below a private runtime root with its hash and benchmark report. The pipeline never applies a candidate to the active Skill and has no direct-to-main mode. A separate PR helper may copy an accepted candidate to a new review branch and open a PR with the report; the user merges it.

## Benchmark runner

Use a private fixture root outside this repository. A runner receives `SKILLOPT_SKILL_PATH` and `SKILLOPT_RUN_LABEL`, executes the frozen fixture set, and prints one JSON score object containing total, passed, pass rate, findings-by-code and sentinel failures. The fixture manifest, raw resumes, photos and render output remain private.
