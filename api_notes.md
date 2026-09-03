# 优化器 API 与本地文本空间契约

## API 输入输出

仅在设置 `--execute` 时，`scripts/skillopt_pipeline.py` 才调用 OpenAI 兼容 API。运行时使用 `SKILLOPT_API_KEY`、`SKILLOPT_MODEL` 和可选 `SKILLOPT_BASE_URL`；凭据绝不能进入源代码、日志或样本。

请求由 SkillOpt 系统提示词、脱敏失败摘要和三个可变公开指令章节组成。响应必须是匹配 `OptimizerResponse` 的 JSON：`summary`、`hypothesis`、`patch` 和 `expected_effect`。补丁为 RFC 6902，最多 3 个操作，只能作用于获准版式章节。

## 本地文本空间与晋级

反思期间现役 `SKILL.md` 只读。候选版本连同哈希与基准报告写入私有运行目录。流水线不会应用候选版本到现役 Skill，也没有直接写入 `main` 的模式。独立 PR 助手可把已接受候选复制到评审分支并携带报告创建 PR；是否合并由用户决定。

## 基准运行器

使用仓库外部的私有样本根目录。运行器接收 `SKILLOPT_SKILL_PATH` 和 `SKILLOPT_RUN_LABEL`，执行冻结样本集，并输出包含总数、通过数、通过率、错误码统计和哨兵失败的 JSON。manifest、原始简历、照片和渲染输出保持私有。
