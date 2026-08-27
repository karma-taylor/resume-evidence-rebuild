# Local-only evaluation assets

Use this directory locally for runtime data that must **not** be committed: redacted rollout logs and a frozen 50-fixture A4 benchmark. A recommended layout is:

```text
assets/
├── trajectories/YYYY-MM/a4_qa_failures.jsonl
└── benchmarks/a4-single-page-v1/
    ├── manifest.json
    └── fixtures/  # source materials, render expectations, sentinel cases
```

The public repository contains this README only. Keep all actual resumes, headshots, JDs, project materials, rendered PDFs, and benchmark fixtures outside Git or in an encrypted/private store.
