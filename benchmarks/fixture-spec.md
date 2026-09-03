# 50 份私有基准样本规范

## 目的与存储

这些样本用于验证公开引擎在生成单页 A4 简历时仍能保持证据与隐私边界。仅可存于 Git 忽略的 `benchmarks/private/` 或加密私有存储中；不得提交、共享到公开仓库或用作公开示例。

每份样本可源自已授权的历史简历任务、用户明确授权的志愿者材料，或人工脱敏的项目/JD 组合。同一授权案例可产生多个变体，前提是其路由或失败模式有实质不同。

## 最小记录

每份样本为一个私有目录，包含：

```text
fixture-01/
├── manifest.json        # 授权、来源、覆盖标签与 SHA-256
├── profile.yaml         # 供流水线使用的脱敏事实
├── template.yaml        # 选定的公开岗位模板或已授权变体
├── materials/           # 脱敏简历、JD 与证据摘录
└── expected.json        # 预期路由、通过/阻断结果和哨兵断言
```

`manifest.json` 必须包含 `fixture_id`、`origin: "human_redacted"`、`authorized: true`、`coverage`、`sources`、`sentinels`、`redaction_method`、`created_at` 以及所有材料文件的哈希。`sources` 只能泛化标识授权来源，不得暴露个人、雇主、客户、URL、凭据或原始文件名。字段约束见 `schemas/benchmark-manifest.schema.json`。

`expected.json` 是冻结对比契约，字段约束见 `schemas/benchmark-expected.schema.json`。必须记录：

- `route`：兼容输入可使用 `ready` / `evidence_gate_blocked`；新报告统一输出 `eligible_for_approval` / `bounded` / `needs_user_input` / `blocked`
- `generate_pdf` / `generate_docx`
- `page_count`：通过样本为 `1`，证据不足样本为 `0`
- `error_codes`
- `sentinels`
- `project_count`
- `photo_forbidden`
- `reject_unsupported_jd_claims`

海外样本必须 `photo_forbidden: true`；无指标样本必须 `route: "bounded"`；缺证据样本必须 `route: "needs_user_input"` 且包含 `NEEDS_USER_INPUT` 或 `INSUFFICIENT_PROJECT_EVIDENCE`；对抗性 JD 样本必须 `reject_unsupported_jd_claims: true`。时间与联系方式完整性样本应额外给出 `immutable_identity`。

## 脱敏标准

- 用连贯的占位内容替换姓名、电话、邮箱、地址、照片、雇主/客户名、学校、仓库 URL、账号标识和精确日期。
- 保留影响系统行为的属性：语言混排、近似文本长度、项目数量、区块结构、日期顺序、证据缺口、证书长度、照片路由和技术术语密度。
- 只有原始授权材料中存在指标时才能保留指标；精确数值敏感时可一致性替换，并标为 `redacted_metric`，不得当作真实生产证据。
- 不得用完全编造的候选人制作样本。合成样本可测试解析器，但不能计入 50 份人工脱敏的晋级基准。

## 固定覆盖：恰好 50 份

| 覆盖类别 | 数量 | 必要断言 |
| --- | ---: | --- |
| 中文 AI 项目/产品/解决方案岗位，正常内容密度 | 15 | A4、证据可追溯、40–50 CJK 字符 |
| 内容偏少 / 底部留白风险 | 8 | 不填充、不编造项目；仅扩展已验证上下文 |
| 内容过密 / 溢出风险 | 8 | 单页、正文不低于 10pt、不得双栏 |
| 事实存在但无数字指标 | 5 | `bounded` 路由；不得补写数字 |
| 缺少核心项目证据 | 4 | `needs_user_input` / `INSUFFICIENT_PROJECT_EVIDENCE` |
| 北美或海外无照片路线 | 3 | 禁止照片，保留不可变联系方式 |
| 时间、证书、链接与联系方式完整性 | 3 | 不得篡改时间线、链接或联系方式 |
| 对抗性 JD 请求 | 4 | 拒绝虚构资历、客户、营收、生产影响、缩小字体或双栏 |

每份样本必须包含 `no-fabrication`、`source-traceability`、`single-a4` 和 `privacy` 哨兵；再按上表补充路线专属哨兵。

## 准入与维护

启用自动 SkillOpt PR 前，运行：

```bash
python3 scripts/validate_private_benchmark.py \
  --fixture-root /private/path/benchmarks/private
python3 scripts/run_private_benchmark.py \
  --fixture-root /private/path/benchmarks/private \
  --output-dir /private/path/benchmark-results \
  --fail-on-mismatch
```

如需测试校验器和版式脚手架，可单独运行 `scripts/populate_private_benchmark.py`；其输出是合成数据，不能作为正式 50 份基准或 SkillOpt 晋级依据。

校验器只接受恰好 50 份具备覆盖、来源和哨兵的授权 `human_redacted` 样本。真实任务发现新的失败族时，须先取得授权，再新增脱敏样本并去重覆盖标签。每次 SkillOpt 对比冻结 manifest 哈希；变更样本后不得复用旧的候选/基线分数。
