# 验证与恢复规则

## 门控裁决

SkillOpt 候选版本只有在完整、冻结的私有基准上运行，并同时满足以下条件时才可创建 PR：

1. 证据、隐私、时间一致性和防编造哨兵均为**零违规**。
2. 没有任何既有安全样本回归。
3. A4 QA 通过率**严格高于**基线；或通过率不变、零回归且目标错误码更少。
4. 运行记录包含样本 manifest 哈希、候选/基线哈希、补丁、分数和错误码对比。

接受的候选只写入评审分支并创建 PR，绝不覆盖 `main`，必须人工合并。后续哨兵回归将回滚至上一接受版本并关闭自动晋级。

## Data Probe 路由

| 状态 | 含义 | 下一步 |
| --- | --- | --- |
| `ready` | 已授权事实、来源、范围和时间齐全，且存在可声明的指标。 | 正常生成。 |
| `bounded` | 有事实但缺数字指标，或适用范围受限。 | 仅使用交付机制、验证方式或范围描述；不得编数字。 |
| `needs_user_input` | 缺少核心事实、授权、来源或时间。 | 停止并生成精确补问清单。 |
| `evidence_gate_blocked` | Claim 未授权、跨项目、kind 不匹配或原文不能逐字支持断言。 | 立即停止；不得让 Agent B 重写掩盖事实错误。 |

## 自动诊断与 SkillOpt 路由

每次门控失败先进入 `output/quarantine/<run_id>`，并生成脱敏的
`skillopt-event.json`，统一进入 SkillOpt Controller。`scripts/skillopt_auto_loop.py diagnose` 根据错误码生成根因摘要。

- 可优化错误仅限公开版式/交付规则：`PARAGRAPH_SPACING_ERROR`、`PAGE_SIZE_ERROR`、`MARGIN_OUT_OF_RANGE_ERROR`、`MULTI_COLUMN_LAYOUT_ERROR`、`VISUAL_DESIGN_MISMATCH_ERROR` 及可证明属于版式的 `DELIVERY_GATE_BLOCKED`。
- `evidence_gate_blocked`、`content_gate_blocked`、`BOTTOM_WHITESPACE_EXCESS`、`PAGE_COUNT_ERROR`、Claim/授权/字数错误也会进入 Controller，但只能走 `evidence_review` 或 `content_recovery`，生成恢复请求/补问，不得修改保护规则或编造事实。
- 提供私有 runtime 和活动 Skill hash 后自动执行 Controller；公开规则候选还需要冻结 benchmark。设置 `SKILLOPT_AUTO_ENABLED=0` 可停用自动调度；未配置时事件保持 `queued`。
- 候选只能修改公开版式章节，验证失败自动 `rejected` 并冷却；正式 `SKILL.md` 默认不变，必须人工评审合并。
- canary 若发生哨兵回归，删除 `active_candidate.json` 并记录 `rollback/<event_id>.json`，恢复 incumbent hash。

## 确定性恢复

| 错误 | 允许修复 | 禁止修复 |
| --- | --- | --- |
| `PAGE_COUNT_ERROR` / `OVERFLOW` | 先按 `normal → compact_1 → compact_2` 压缩；仍超页后进入 `content_recovery`，先用冻结的 `compressed` 30–40 CJK 档重组；若仍超页且项目数超过 3，再候选删除 JD 排名最低的整个项目并重跑 Agent A/B 与全部 QA；只剩 3 个项目时请求用户选择。 | 自动删保留项目的要点、截断句子、缩字号、改主题。 |
| `BOTTOM_WHITESPACE_EXCESS` | 先走 SkillOpt Controller 的 `content_recovery`：检查未使用授权 Claim，使用冻结的 `expanded` 50–130 CJK 档生成受证据约束的 Agent B 扩写/重组候选，并可按 JD 证据增加项目后重新跑 QA；候选耗尽后 `content_gate_blocked + needs_user_input`。 | 凭空扩写、装饰性填充、进入 compact。 |
| `BUSINESS_CONTEXT_MISSING` / `BUSINESS_ACTION_MISSING` / `BUSINESS_RESULT_MISSING` | 进入 SkillOpt `content_recovery`，优先扫描同项目未使用的已授权业务 Claim；仍无上下文、动作或结果时生成精确补问。 | 用技术名词冒充业务价值、从 summary 拆写、编造职责或指标。 |
| `TECHNICAL_TERM_OVERLOAD` / `TECHNICAL_TERM_PLACEMENT_ERROR` / `BUSINESS_READABILITY_ERROR` | 进入 SkillOpt `content_recovery`，在同一 Claim 集内重组：背景保持业务语言，技术只留在解决动作，结果保留授权指标/交付结论；候选必须重新通过完整证据与版式 QA。 | 通过增加未经授权的连接句、泛化形容词或新增技术栈绕过业务门控。 |
| `FONT_TOO_SMALL_ERROR` | `layout_gate_blocked`；停止。overview 唯一可为 9pt，其他正文/日期/联系人至少 10pt。 | 缩小正文或突破模板边距。 |
| `BULLET_LENGTH_ERROR` | 仅允许同一组 Claim 的 Agent B 格式重写，首次后最多两次。 | 自动添加、删除或改变事实。 |
| `BULLET_BOLD_MISSING_ERROR` | 加粗同一要点中已有证据支持的指标、架构、控制或交付边界。 | 加粗无证据说法或整条要点。 |
| `COMPLIANCE_PHOTO_ERROR` | 按模板市场路由处理；需照片时请求授权合规照片。 | 伪造照片或静默覆盖市场路由。 |
| `INSUFFICIENT_PROJECT_EVIDENCE` | 请求授权本地/GitHub 证据，或交付证据缺口报告。 | 用模型知识创造经历。 |

仅 bullet 的长度、末尾位置、加粗字段与文本不一致可在首次输出后重试两次。来源、授权、kind、跨项目、裁切、重叠、字号、实际加粗或哈希错误均为零重试阻断；每轮记录产物哈希、元素 ID 和物理发现。
