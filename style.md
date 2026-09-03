# Rollout 日志与有界编辑格式

## 私有失败轨迹

每次 A4 失败任务都写入私有运行目录。JSON 记录哈希、相对引用、当前公开规则快照和结构化 QA 发现；已授权原始材料复制到受访问控制的 `inputs/` 子目录，绝不提交。

```json
{
  "schema_version": "1.1",
  "event_type": "a4_qa_failed",
  "run": {"run_id": "rollout-...", "skill": {"sha256": "..."}},
  "qa": {"passed": false, "findings": [{"code": "BOTTOM_WHITESPACE_EXCESS", "element_id": "project.rag.bullet.3"}]}
}
```

## 有界补丁封装

优化器对 `workflow`、`output-routing` 和 `one-page-layout-qa` 三个章节字符串返回 RFC 6902 补丁。仅可使用 `add`、`remove` 或 `replace`；最多 3 个操作，合计字符变化不超过 450。不得修改 frontmatter、证据/隐私规则、Schema、模板、基准定义、身份数据或来源。

## 要点载荷

每条生成项目要点均为对象：

```json
{
  "text": "一条介于四十至五十个中文字符、仅含已授权事实的业务项目描述。",
  "bold_phrases_used": ["已验证控制规则"],
  "source_claim_ids": ["claim-id"]
}
```

首次 `normal` 输出的项目 `text` 含 **40–50 个 CJK 字符**；超页恢复可使用冻结的 `compressed` 30–40 档，过疏恢复可使用冻结的 `expanded` 50–70 档，且必须在 `typeset-plan.json.content_mode` 中声明。工作经历始终为 40–50。所有档位都按“业务背景／难点 → 解决动作 → 可量化业务结果”组织；`bold_phrases_used` 含 1–2 个逐字出现于 `text` 的字符串，量化结果必须位于句末且加粗。每个短语都必须通过 `source_claim_ids` 回溯到已授权事实。渲染器必须将这些精确短语实际渲染为粗体。
