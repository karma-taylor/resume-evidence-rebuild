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

## 公开冒烟测试

安装依赖、Typst 和 `Microsoft YaHei` 后，可运行 `python3 scripts/run_smoke_test.py` 检查虚构样例的证据门、Agent A/B Schema、单页 PDF 与交付 manifest。完整安装说明见 [INSTALL.md](INSTALL.md)；真实简历仍应使用 Codex 对话中的私有材料入口。
