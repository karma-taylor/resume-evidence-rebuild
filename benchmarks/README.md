# 私有基准契约

权威基准位于 Git 忽略的 `benchmarks/private/`，启用自动晋级前必须拥有恰好 50 份**人工脱敏**样本。不得提交候选人简历、照片、JD、源仓库、生成 PDF 或评测输出。

`scripts/init_private_benchmark.py` 只能创建带标签的空脚手架；脚手架会被校验器拒绝，不能算作基准证据。`scripts/populate_private_benchmark.py` 只生成合成占位样本，不能标记为 `human_redacted`，也不能计入正式 50 份基准。正式样本应放入独立私有目录，通过 `scripts/validate_private_benchmark.py` 和 `scripts/run_private_benchmark.py` 验收。覆盖比例固定为 15/8/8/5/4/3/3/4，另有四类基础哨兵和材料 SHA-256。`expected.json` 的正式字段见 `schemas/benchmark-expected.schema.json`。完整要求见 [fixture-spec.md](fixture-spec.md)。

已审核的 50 份基准应由私有 runner 通过 `.github/workflows/private-benchmark.yml` 触发，不能上传到公共 CI。
