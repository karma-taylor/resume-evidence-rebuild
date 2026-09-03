# 仅本机的评测资产

此目录可在本机保存**不得提交**的运行数据：脱敏 rollout 日志与冻结的 50 份 A4 基准。推荐布局：

```text
assets/
├── trajectories/YYYY-MM/a4_qa_failures.jsonl
└── benchmarks/a4-single-page-v1/
    ├── manifest.json
    └── fixtures/  # 源材料、渲染预期和哨兵案例
```

公开仓库只保留此说明。真实简历、照片、JD、项目材料、渲染 PDF 和基准样本必须保留在 Git 外部或加密私有存储中。
