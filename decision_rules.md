# Validation and recovery rules

## Gate decision

A SkillOpt candidate is eligible for a pull request only when it has run the complete, frozen private benchmark manifest and all of the following are true:

1. Evidence, privacy, time-consistency and anti-fabrication sentinels have **zero violations**.
2. No existing safety fixture regresses.
3. A4 QA pass rate is **strictly higher** than the incumbent, or the pass rate is unchanged with zero regressions and fewer occurrences of the target error code.
4. The run records fixture-manifest hash, candidate and incumbent hashes, patch, score and error-code comparison.

An accepted candidate is written only to a review branch and used to create a PR. It never overwrites `main`; a human merge is required. A later sentinel regression triggers rollback to the previous accepted version and disables automated promotion.

## Data Probe routing

| Probe state | Meaning | Next action |
| --- | --- | --- |
| `ready` | Authorized facts, source, scope and time are present; a metric is present where claimed. | Generate normally. |
| `bounded` | Facts are present but a numeric metric is absent or scope is limited. | Use delivery mechanism, validation method or scope wording; never invent a number. |
| `needs_user_input` | Core fact, authorization, source or time is missing. | Stop and produce an exact question list. |

## Deterministic output recovery

| Error | Allowed correction | Forbidden correction |
| --- | --- | --- |
| `PAGE_COUNT_ERROR`, `PAGE_SIZE_ERROR`, `OVERFLOW` | Keep A4 and one column; remove the least material verified detail or reselect a lower-priority project. | Shrinking body type below 10 pt, changing page size, inventing results. |
| `BOTTOM_WHITESPACE_EXCESS` | Expand an already verified project's business context, decision boundary, risk control or validation scope; re-render. | Adding a project or metric without evidence; decorative filler. |
| `FONT_TOO_SMALL_ERROR` | Restore at least 10 pt, then reduce secondary verified detail. | Reducing body font or margins beyond template bounds. |
| `BULLET_LENGTH_ERROR` | Below range: add a source-backed context/control/scope. Above range: remove modifiers and redundant detail. | Altering a fact or a numeric result. |
| `BULLET_BOLD_MISSING_ERROR` | Bold one or two already-sourced metric, architecture, control or delivery-boundary phrases. | Bolding an unsupported claim or a whole bullet. |
| `COMPLIANCE_PHOTO_ERROR` | Apply the template market route; request an authorized compliant photo if one is required. | Fabricating a photo or silently overriding the market route. |
| `INSUFFICIENT_PROJECT_EVIDENCE` | Ask for authorized local/GitHub evidence or deliver an evidence-gap report. | Creating experience from model knowledge. |

Two automatic content retries are allowed for one error family. Each retry stores the artifact hash, element IDs, physical findings and edits in the private runtime directory. A source, authorization or privacy failure is blocking and is not retried with generated content.
