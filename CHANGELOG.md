# Changelog

## 1.0.8 — 2026-09-03

- Adjusted the authoritative Typst PDF top margin from 1.27cm to 1.35cm so the measured ink boundary stays above the 36pt QA floor for both Microsoft YaHei and the redistributable Noto CJK smoke font. The physical margin remains within the documented 1.27–2.54cm range.
- This is a geometry-contract correction, not a QA relaxation or a production-font change; it makes the public and production font metrics share the same fixed layout contract.
- Root cause was confirmed after 1.0.7: `pdftoppm` was available, and the remaining public failure was Noto's 34.4pt top ink boundary.
- Verified the generated Typst comment syntax locally before publishing the release.

## 1.0.7 — 2026-09-03

- Fixed the public Ubuntu PDF smoke environment by installing `poppler-utils`, which provides the `pdftoppm` binary required for raster bold QA.
- `environment_doctor.py` now checks `pdftoppm` and reports it under `pdf.pdftoppm`, failing early with an actionable message when Poppler is missing.
- Root cause was confirmed by the 1.0.6 CI diagnostics: Python 3.11–3.13 passed, and the render failed at `PDF_BOLD_NOT_RENDERED_ERROR` because `pdftoppm` was unavailable.

## 1.0.6 — 2026-09-03

- Extended public PDF smoke diagnostics to include a truncated, path-redacted final render reason when the renderer fails before writing geometry QA. Resume text, private paths, and generated artifact contents remain excluded.
- Recorded the third public CI verification result: Python 3.11–3.13 passed, while the Noto CJK public render still returned `RENDER_ERROR`; this release does not claim a green public CI until that failure is fixed.

## 1.0.5 — 2026-09-03

- Fixed public smoke failure diagnostics to inspect both the output root and `quarantine/<run_id>/`, exposing only the actual gate code and other non-sensitive metadata.

## 1.0.4 — 2026-09-03

- Improved public PDF smoke diagnostics with route, Reflow state, layout state, page count, and error codes only; resume text, private paths, and generated artifacts remain excluded.

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
