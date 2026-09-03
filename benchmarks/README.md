# 私有基准契约

权威基准位于 Git 忽略的 `benchmarks/private/`，启用自动晋级前必须拥有恰好 50 份**人工脱敏**样本。不得提交候选人简历、照片、JD、源仓库、生成 PDF 或评测输出。

`scripts/init_private_benchmark.py` 只能创建带标签的空脚手架；脚手架会被校验器拒绝，不能算作基准证据。每份完成的样本都必须声明 `human_redacted` 来源、覆盖维度、授权来源、哨兵预期和稳定 SHA-256 manifest。完整要求见 [fixture-spec.md](fixture-spec.md)。
