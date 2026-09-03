# Changelog

## 1.0.3 — 2026-09-03

- Added public PDF smoke CI and a private self-hosted 50-fixture benchmark workflow.
- Added machine-readable environment diagnostics, optional DOCX dependency checks, and explicit test-font overrides.
- Added strict route/artifact mismatch failure for private benchmark automation.
- Separated synthetic fixtures from authorized human-redacted benchmark manifests.
- Expanded README and INSTALL with material intake, JD hashing, rendering, quarantine, and private benchmark procedures.

## 1.0.2 — 2026-09-03

- Unified private benchmark directories (`fixture-01/` … `fixture-50/`) with `manifest.json` and `expected.json` schemas.
- Private validator now checks coverage counts, sentinels, material SHA-256, and redaction constraints.
- Added local populate and baseline runners for the Git-ignored 50-fixture corpus.

## 1.0.1 — 2026-09-03

- Added a public synthetic fixture, source-material hashes, and schema validation.
- Added a safe PDF smoke test and deterministic Microsoft YaHei environment check.
- Hardened DOCX delivery-manifest verification and SkillOpt benchmark command execution.
- Added release, security, contribution, and CI documentation.

## 1.0.0 — 2026-09-02

- Initial public release of the evidence-first resume reconstruction Skill.
