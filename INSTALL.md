# 社区安装与本地环境

## 用户需要提供什么

在 Codex 对话中提供现有简历、已授权本地项目路径或 GitHub 仓库、JD、可选照片与事实补充。Skill 会建立本地私有档案，用户无需预先创建 YAML。

不要把真实简历、私有档案、照片、JD、API 密钥、生成的 PDF/DOCX、基准样本或运行轨迹放进公开克隆仓库。

## 安装

将仓库克隆到本机 Codex skills 目录，并将目录命名为 `resume-evidence-rebuild`。保留提供的 `.gitignore`。使用执行脚本的 Python 安装依赖：

```bash
python3 -m pip install -r scripts/requirements.txt
```

从 Typst 官方发行版安装 Typst，并确保 `typst` 位于 `PATH`。中文简历需要同时具备常规与粗体字形的 `Microsoft YaHei` 字体；这是当前 Typst 与 DOCX 渲染器共同使用的确定性字体。

PDF 几何与粗体栅格 QA 还需要 Poppler 的 `pdftoppm`：macOS 可通过 `brew install poppler` 安装，Debian/Ubuntu 可通过 `sudo apt-get install poppler-utils` 安装。`environment_doctor.py` 会在渲染前检查该命令。

## 验证环境

```bash
python3 scripts/environment_doctor.py
python3 scripts/validate_resume_artifacts.py --skill-dir .
```

`environment_doctor.py --json` 可用于 CI 或部署前检查；生产默认要求 `Microsoft YaHei`。公共 CI 若使用可再分发的测试字体，必须显式设置 `RESUME_CJK_FONT`，该设置只用于结构与渲染冒烟，不改变生产默认字体：

```bash
python3 scripts/environment_doctor.py --json
RESUME_CJK_FONT="Noto Sans CJK SC" \
  python3 scripts/environment_doctor.py --font "Noto Sans CJK SC" --json
```

DOCX 额外交给 LibreOffice 做实际渲染和 OOXML 检查：

```bash
python3 scripts/environment_doctor.py --require-docx --json
```

前者检查 Python 包、Typst 与可用 CJK 字体；后者检查 Skill 必需文件和 frontmatter。

## 虚构样例冒烟测试

示例仅包含虚构数据。以下命令会自动生成 Agent A/B 文案并生成本地 PDF：

```bash
python3 scripts/build_resume.py \
  --profile examples/sample.profile.yaml \
  --template examples/sample-template.yaml \
  --inbox examples/sample-inbox.yaml \
  --output-dir /tmp/resume-evidence-demo \
  --render --theme-variant executive_editorial_a
```

也可以运行公开冒烟测试（它使用隔离临时目录，并检查 PDF、manifest 与 Schema）：

```bash
python3 scripts/run_smoke_test.py
```

DOCX 是可选交付格式，建议在安装 LibreOffice 后单独使用 `build_resume.py --docx` 验证。不得将该示例档案作为真实简历使用。

## 私有 50 份基准

正式私有基准必须放在 Git 忽略的独立目录，并通过环境变量或命令行传入：

```bash
export RESUME_PRIVATE_BENCHMARK_ROOT=/private/path/benchmarks/private
python3 scripts/validate_private_benchmark.py \
  --fixture-root "$RESUME_PRIVATE_BENCHMARK_ROOT"
python3 scripts/run_private_benchmark.py \
  --output-dir /private/path/benchmark-results \
  --fail-on-mismatch
```

`scripts/populate_private_benchmark.py` 只生成解析和版式测试用的合成数据，不能替代已授权的人工脱敏基准。私有验证工作流需要自托管 runner、生产字体和私有基准目录；它只保留测试摘要，不上传简历、JD、PDF、DOCX 或 quarantine 正文。
