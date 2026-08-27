# Private benchmark contract

The authoritative benchmark lives in an ignored directory, normally `benchmarks/private/`. It must contain exactly 50 **human-redacted** fixtures before automated promotion is enabled. Do not commit candidate resumes, photos, JDs, source repositories, generated PDFs or evaluation output.

Use `scripts/init_private_benchmark.py` only to create labeled empty scaffolds. Scaffolds are deliberately rejected by the verifier and do not count as benchmark evidence. Each completed fixture must declare its origin as `human_redacted`, its coverage dimensions, authorized sources, sentinel expectations and a stable SHA-256 manifest entry.
