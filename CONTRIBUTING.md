# Contributing

Contributions should preserve the evidence-first contract: no invented resume facts, no real personal data in fixtures, and no weakening of provenance or artifact gates for convenience.

Before opening a pull request, run:

```bash
python3 scripts/validate_examples.py
python3 scripts/environment_doctor.py
python3 -m compileall -q scripts tests
python3 -m pytest -q
python3 scripts/run_smoke_test.py
```

Changes that affect layout, evidence routing, or SkillOpt must include a focused regression or adversarial test and update the changelog when user-visible.
