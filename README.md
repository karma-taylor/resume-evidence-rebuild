# 简历证据重构

这是一个以证据为先的 Codex Skill，用于稳定生成单页 A4 简历。公开仓库仅包含可复用规则、Schema、岗位模板、确定性 QA 与虚构示例。

社区用户默认使用**零配置模式**：在 Codex 对话中上传现有简历、已授权项目材料和 JD。Skill 从材料中提取证据，并在本机生成 Git 忽略的 `profiles/private.yaml`；用户无需手写 YAML。该私有档案保存真实身份、联系方式、照片、教育、工作、项目证据和指标。高级用户也可以自行维护。

处理链路为：`授权材料 → 私有档案 → 岗位模板 → Data Probe → Agent A → Agent B → Typst PDF → 几何 QA → 原子发布`。任何失败都会进入 quarantine 和统一 SkillOpt Controller；版式错误生成规则候选，内容错误扫描未使用授权 Claim 并生成重组候选，证据不足时生成精确补问。候选只有在私有基准验证通过后才能创建待评审 PR，绝不自动覆盖活动 Skill。

构建前置校验（即使没有 `--render`）发生异常时同样会创建隔离的失败事件并交给 SkillOpt Controller；只有带通过 manifest 的文件才属于交付物。

自动闭环入口：

```bash
python scripts/skillopt_auto_loop.py diagnose \
  --failed-manifest /path/to/output/quarantine/<run_id>/failed-manifest.json \
  --runtime-root /private/runtime/resume-skillopt
```

如果已有结构化档案、岗位 JD 和本地项目目录，也可以直接让构建器完成“扫描项目 → 按 JD 选项目 → 生成首版文案 → Reflow → A4 QA”：

```bash
python scripts/build_resume.py \
  --profile /private/path/profiles/private.yaml \
  --template templates/ai-project-manager.yaml \
  --inbox /private/path/ingestion_inbox.yaml \
  --jd-brief /private/path/jd-brief.json \
  --project-dir daily-digest=/private/path/Daily_digest \
  --project-dir hybrid-rag=/private/path/hybrid-rag \
  --project-dir work-calendar=/private/path/work-calendar \
  --output-dir /private/path/output \
  --render --theme-variant executive_editorial_a
```

省略 `--agent-b-output` 时，系统会基于已授权 Claim 生成确定性的首版 Agent B 文案；这不是自由扩写，项目仍固定为三段式，工作经历则直接生成 4–5 条业务导向 bullet，所有文字仍必须逐字回溯到授权来源。项目路径只读扫描并写入 staging，不能把代码或 README 当成新的简历事实。

正文默认面向非技术业务读者：项目按“业务背景 → 解决动作 → 已验证结果”组织，技术术语只保留在解决动作中且每条最多 2 个；工作经历直接说明业务对象、动作和结果，技术栈集中放在技术能力模块。缺少业务上下文、动作或结果证据时会阻断并进入受控 SkillOpt 内容恢复，不会用未经授权的价值描述或指标填充。

自动 Controller 默认使用 Skill 目录下 Git 忽略的 `.skillopt-runtime` 与当前 `SKILL.md`；也可提供私有 `SKILLOPT_RUNTIME_ROOT` 与 `SKILLOPT_SKILL_PATH` 覆盖。公开规则候选另需冻结的 `SKILLOPT_BENCHMARK_COMMAND`。证据和内容密度错误也会进入 Controller，但只生成恢复请求或补问，不修改保护规则。
未配置外部 benchmark 时，公开版式错误也不会停在“排队”：Controller 会生成 `candidate_pending_validation` 离线有界候选，等待冻结基准验证；候选不会自动覆盖活动 `SKILL.md`。

社区环境请阅读 [安装说明](INSTALL.md)；零配置建档路线见[此处](references/zero-config-intake-plan.md)；启用 SkillOpt 前请阅读 [50 份基准样本规范](benchmarks/fixture-spec.md)。

## 更新记录

### 1.0.3 — 2026-09-03

本版本完成公开发布前的验证链路加固：

- 统一 Data Probe、Agent B、Reflow、交付门禁和 benchmark runner 的结果契约为 `eligible_for_approval`、`bounded`、`needs_user_input`、`blocked`；旧版 `ready` 及历史 gate 状态只在输入归一化时兼容，不再作为新的内部结果输出。
- 修复 Agent B 的工作经历 fallback：`architecture`、`control`、`delivery` 结果在没有数字指标时也可以形成合法业务 bullet；只有完全没有合格结果 Claim 时才请求用户补充。
- 修复目录型 Validation Gate：以包含合法 `manifest.json` 的 fixture 目录作为唯一原子样本，忽略 `expected.json`、`PROCESSING-STATUS.json` 和运行报告，并输出统一的 `BenchmarkScore`，包括总数、通过数、A4 通过率、错误码统计和安全哨兵。
- 增加私有 50 份 runner：每个 fixture 使用隔离运行目录；单个样本超时或失败不会中止全量执行；PDF、DOCX、route、artifact 和 error code 均进行确定性核对；私有简历、JD、profile、trace 和生成物不上传公共 CI artifact。
- 完善证据恢复和版式 Reflow：压缩、扩写、整项目裁剪均只能使用已授权 Claim；每次恢复保留 trace；清除 stale quarantine 对当前结果的影响；禁止用字体缩小、双栏或未绑定文案绕过门禁。
- 修复 DOCX 交付链：保持 10pt 正文、精确 OOXML 行距和相邻 bullet 间距，使用 LibreOffice 实际渲染检查单页 A4；DOCX 失败只返回 `blocked`，不推翻已经通过的 PDF。
- 增加公共 CI 的 PDF smoke、环境检查和本机绝对路径检查；增加手动触发的私有 self-hosted runner workflow；补充 Typst 0.15.1、可再分发测试字体、Microsoft YaHei 和 LibreOffice 的环境策略。
- 本轮本地验收：71 个测试通过；公共 smoke、技能产物 QA、私有基准结构校验通过；50 份实际行为验证连续两轮均为 `50/50`，A4 QA 为 100%，安全哨兵失败为 0，逐 fixture 结果完全一致。

### 1.0.2 — 2026-09-03

- 将私有基准统一为 `fixture-01/` 至 `fixture-50/` 目录，每个目录包含 `manifest.json`、`expected.json`、`profile.yaml`、`template.yaml` 和 `materials/inbox.yaml`。
- 增加 manifest、expected 和 synthetic manifest Schema；校验覆盖类型、路由、sentinel、材料哈希、脱敏标记和私有/合成来源边界。
- 增加私有基准初始化、脱敏标准化和本地运行脚本；明确合成样本不能冒充已审核的人类脱敏样本。

### 1.0.1 — 2026-09-03

- 增加公开合成 fixture、源材料哈希和 Schema 校验。
- 增加安全 PDF smoke、确定性字体/环境检查和 DOCX delivery manifest 校验。
- 加固 SkillOpt benchmark command、quarantine、发布、贡献和 CI 文档。

### 1.0.0 — 2026-09-02

- 首次公开发布证据优先的简历重构 Skill，提供私有事实档案、JD 驱动项目选择、Agent A/B、Typst 单页 A4 PDF 和确定性证据/版式门禁。

## 公开冒烟测试

安装依赖、Typst 和 `Microsoft YaHei` 后，可运行 `python3 scripts/run_smoke_test.py` 检查虚构样例的证据门、Agent A/B Schema、单页 PDF 与交付 manifest。完整安装说明见 [INSTALL.md](INSTALL.md)；真实简历仍应使用 Codex 对话中的私有材料入口。

## 从材料到交付：完整操作

### 1. 建立并审核事实池

对已有文本、PDF 或 DOCX，先扫描到 Git 忽略的 inbox；扫描结果只是候选事实，不能直接进入简历：

```bash
python3 scripts/ingest_resume.py scan \
  --source /private/path/resume.pdf \
  --employer "已确认的雇主名称" \
  --inbox /private/path/ingestion_inbox.yaml \
  --ingestion-id resume_20260903
```

检查 `ingestion_inbox.yaml` 后，仅批准用户确认的 ingestion ID：

```bash
python3 scripts/ingest_resume.py approve \
  --inbox /private/path/ingestion_inbox.yaml \
  --profile /private/path/profiles/private.yaml \
  --ingestion-ids resume_20260903_01 resume_20260903_02
```

普通用户不需要手写这些文件；在 Codex 中上传材料并明确确认即可。CLI 只适合已经有私有档案的高级用户。

### 2. 创建 JD 简报并绑定本地证据

JD 文本本身存放在 Git 忽略目录，例如 `/private/path/job-description.txt`。先计算原文哈希，再写入符合 `schemas/jd-brief.schema.json` 的 JSON：

```bash
python3 - <<'PY'
import hashlib, pathlib
path = pathlib.Path('/private/path/job-description.txt')
print(hashlib.sha256(path.read_bytes()).hexdigest())
PY
```

最小 `jd-brief.json` 示例：

```json
{
  "schema_version": "1.0",
  "target_role": "企业 AI 项目经理",
  "jd_text_sha256": "将上一步输出填入这里",
  "max_projects": 3,
  "requirements": [
    {
      "id": "req-01",
      "text": "推动跨团队 AI 项目交付",
      "keywords": ["项目交付", "跨团队"],
      "priority": "required"
    }
  ]
}
```

只对用户明确提供的本地项目目录执行只读扫描：

```bash
python3 scripts/jd_project_selector.py scan \
  --jd-brief /private/path/jd-brief.json \
  --project-dir project-a=/private/path/project-a \
  --project-dir project-b=/private/path/project-b \
  --output /private/path/jd-evidence-map.json
```

### 3. 生成 PDF 和可选 DOCX

```bash
python3 scripts/build_resume.py \
  --profile /private/path/profiles/private.yaml \
  --template templates/ai-project-manager.yaml \
  --inbox /private/path/ingestion_inbox.yaml \
  --jd-brief /private/path/jd-brief.json \
  --jd-evidence-map /private/path/jd-evidence-map.json \
  --output-dir /private/path/output \
  --render --theme-variant executive_editorial_a
```

只有 PDF 通过证据、字体、几何和单页 QA 后，才允许追加 `--docx`。`delivery-manifest.json` 才是可交付依据；`resume.pdf` 或失败目录中的文件不能直接当作交付物。

### 4. 失败处理

| 状态或错误 | 处理 |
| --- | --- |
| `eligible_for_approval` | 检查交付 manifest 后进入人工批准；旧输入 `ready` 会归一化为此状态 |
| `bounded` | 保留证据边界，不能补写指标 |
| `needs_user_input` | 补充或确认缺失事实后重新构建 |
| `blocked` | 检查 Claim、来源哈希、JD 映射、授权状态或物理交付门控；旧 `evidence_gate_blocked` / `layout_gate_blocked` / `delivery_gate_blocked` 会归一化为此状态 |
| `quarantine/` | 只读取脱敏诊断；不要把其中内容复制到公开仓库 |

## 自动化验证

公开 CI 只运行合成样例、Schema、单元测试和公开 PDF 冒烟；不读取任何候选人档案。授权的 50 份私有基准通过本机或私有 runner 的 `private-benchmark.yml` 运行，基准正文和生成物不会上传。

本地私有验证：

```bash
python3 scripts/validate_private_benchmark.py \
  --fixture-root /private/path/benchmarks/private
python3 scripts/run_private_benchmark.py \
  --fixture-root /private/path/benchmarks/private \
  --output-dir /private/path/benchmark-results \
  --fail-on-mismatch
```

基准验证通过后，SkillOpt 仍须经过 `scripts/skillopt_validation_gate.py` 和人工评审；不会自动覆盖活动 `SKILL.md`。
