# Herdr Team Manager v4.2.0-beta.1

[MIT License](LICENSE) · **Beta / Pre-release，未经真实 Herdr 全流程验收，不适用于生产无人值守运行。**

**本次目标：修复 v4.1 独立审计中的派单、结果回收、配置复用、并行互斥、异常恢复及安装问题。** 保留 `/herdr-team` Skill、七模板、YAML/JSON 配置和 Herdr TUI。**本版本不提供真正自主运行的后台 Lead，也不具有系统级沙盒/Owner 身份认证。**

## 先看结论

- **适合：** 隔离 Mock 测试、无敏感数据的 Herdr CLI 联调、独立测试 Git 仓库中的有限试运行。
- **不适合：** 在未经沙盒隔离的情况下运行 私人原始数据/生产仓库或高危自动化；不适合无人值守地保证 Lead 永不提前退出。
- **已完成：** 以 Run/Dispatch ID 绑定的 Worker JSON 工件、未知/旧输出不能结算、跨 Run Worker 互斥、完整配置复用校验、关键测试 Gate、只读恢复视图、取消操作、Hermes 参数收紧、Worktree dirty 预检、隔离升级回滚。
- **仍未实现：** 独立的后台模型监督循环、真实 Owner 审批服务、OS 沙盒强制隔离、真实 Herdr/Hermes 目标环境的端到端验收。配置中的 `escalate/report` 主要是 Lead 汇报指导，并非独立消息通知。

## 功能与核心架构

本项目是可由 Claude Code / Codex 等兼容 CLI Agent 安装的 `herdr-team` Skill，而非独立模型服务。当前 Lead 协议和六个基础模板优先使用 Claude Code；Codex 安装兼容不等于 Codex Lead 已通过真实运行验收。

`Owner → Lead（SKILL.md）→ Python Runner（scripts/htm.py）→ Herdr Pane 中的 Worker`

Lead 负责推理、派单、核验证据和决定 Gate；Runner 负责确定性阶段管理、进程锁、状态保存、命令派发与 Run/Dispatch 绑定的 JSON 工件校验。运行状态默认位于 `~/.local/state/herdr-team/`，不应纳入版本控制。

## 包内结构

```text
herdr-team/
  SKILL.md                      Lead 操作协议，保留 /herdr-team
  scripts/htm.py                确定性运行层和结果工件校验
  scripts/bootstrap-config.sh   安全初始化用户配置
  config/team.yaml              七模板、参数、工作流（JSON-compatible YAML）
  references/operations.md      运行与恢复协议
  references/templates.md       模板说明
  references/worktrees.md      Git Worktree 约束
  tests/test_htm.py             原 18 项更新后的回归
  tests/test_regressions_v42.py 新增攻击/故障反例测试
install-local.sh                离线新装、事务升级、回滚
CHANGELOG.md
LICENSE                         MIT License
pyproject.toml / uv.lock         Python 版本及可选 YAML 依赖
```

## 安装

### 运行依赖

- macOS / Linux（Debian 为目标平台，尚未做真实环境验收），Python 3.9+；源码开发与可选依赖使用 [uv](https://docs.astral.sh/uv/) 管理，禁止向系统 Python 安装依赖。
- 实际多 Agent 运行需要 Herdr TUI、官方 `herdr` Skill、已安装并认证的 Claude Code CLI；作为其他 Lead 使用时需要对应 CLI（如 Codex）。Runner 本身不调用模型 API，也不管理模型凭证。
- Git 用于源码安装、Skills CLI 拉取和可选 Worktree；在线安装需要支持 Skills CLI 的 Node.js / npx。
- `hermes-guide` 另需 Hermes CLI 和专用测试 Profile；其他模板不需要 Hermes。
- 默认 `config/team.yaml` 是 JSON-compatible YAML，**没有必需的第三方 Python 运行依赖**。只有改用传统 YAML 时才需要可选 `PyYAML`。

### 在线全新安装

先安装官方 Herdr Skill（与本项目独立）：

```bash
npx skills add herdrdev/herdr --skill herdr -g
```

正式安装到 Claude Code 和 Codex：

```bash
npx skills add Shakyae/herdr-team \
  --skill herdr-team \
  -g -a claude-code -a codex -y
bash ~/.agents/skills/herdr-team/scripts/bootstrap-config.sh
```

共享目录预期为 `~/.agents/skills/herdr-team/`。Skills CLI 根据 Agent 约定建立链接；Codex 可以直接使用共享目录，不要求额外出现 `~/.codex/skills/herdr-team` 链接。安装输出和实际文件应同时核对。

如要固定本次 Beta，而不是跟随 main，可将来源换成 `https://github.com/Shakyae/herdr-team/tree/v4.2.0-beta.1`。

### 本地源码安装

```bash
git clone https://github.com/Shakyae/herdr-team.git
cd herdr-team
git checkout v4.2.0-beta.1
uv sync --extra yaml
npx skills add . --skill herdr-team -g -a claude-code -a codex -y
bash ~/.agents/skills/herdr-team/scripts/bootstrap-config.sh
```

不使用 Skills CLI 的离线替代方案，会显式建立共享目录与两个软链接（默认配置可直接用 Python 标准库验证）：

```bash
bash install-local.sh
ls -ld ~/.agents/skills/herdr-team ~/.claude/skills/herdr-team ~/.codex/skills/herdr-team
```

Skills 安装器不会安装 Python 依赖。如果采用传统 YAML，在**实际 Skill 目录**运行 `uv sync --extra yaml`，之后用 `uv run --project ~/.agents/skills/herdr-team --extra yaml python ...` 调用 Runner；也可直接使用该目录 `.venv/bin/python`。不能假设系统 `python3` 自动找到 uv 隔离环境中的 PyYAML。

### 从已有安装升级（先检查未结束 Run）

**v4.1 Team/Run 状态不能被 v4.2 自动接管。** 先在旧环境执行旧版 `run status`，确保无在途业务任务；如果未完成，先结束/人工核对旧任务，或换一个全新的测试 Lead Pane 与状态目录。不要把旧 Run 的终端输出转换成已验证工件。

离线脚本支持明确升级并备份旧版本：

```bash
# 从 v4.2 解压后的包根目录运行
bash install-local.sh --upgrade
# 程序会打印旧版备份路径，留存它；如需回滚：
bash install-local.sh --rollback /实际输出的/backup-xxxxxxxx
```

- 升级时先把新 Skill 解压到临时目录、验证配置，再切换 canonical 目录；旧版本备份在 `~/.agents/skills-backups/herdr-team/`。
- 不覆盖已有的 `~/.config/herdr-team/team.yaml`，也不替换已存在的其他 Agent Skill 路径。
- 如果旧版本是 Skills CLI 安装的，**不建议混用两种升级器**。先确认它使用的 canonical 实际路径及软链接后再决定操作；升级脚本默认只管理它识别的 `~/.agents/skills/herdr-team/` 目录。
- 旧 `/team` Skill 保持不动；必要时用户自行停用。**不要删除官方 `herdr` Skill。**
- Skills CLI 安装可用 `npx skills update herdr-team -g -y` 更新；固定 Beta 的用户应显式选择下一个 tag。源码安装用 `git fetch --tags` 后 checkout 新 tag，再运行原安装方式，不要覆盖在途 Run。
- 维护者后续发布：先更新 README / CHANGELOG / Python 版本并完成测试与安全扫描，再正常提交推送；用新 tag 和 `gh release create <tag> --prerelease` 发布下一 Beta。只有完成真实 Herdr 验收后才考虑 Stable，禁止移动已发布 tag 或强推。

## 验证与使用

在项目根目录验证源码（无需安装）：

```bash
uv sync --extra yaml
uv run --extra yaml python scripts/htm.py validate
uv run --extra yaml python scripts/htm.py init dev-review --layout side --dry-run
uv run --extra yaml python -m unittest discover -s tests -v
bash -n install-local.sh
bash -n scripts/bootstrap-config.sh
npx skills add . --list
```

安装后的验证：

```bash
npx skills add Shakyae/herdr-team --list
uv sync --project ~/.agents/skills/herdr-team --extra yaml
uv run --project ~/.agents/skills/herdr-team --extra yaml python ~/.agents/skills/herdr-team/scripts/htm.py validate
uv run --project ~/.agents/skills/herdr-team --extra yaml python ~/.agents/skills/herdr-team/scripts/htm.py templates
uv run --project ~/.agents/skills/herdr-team --extra yaml python -m unittest discover -s ~/.agents/skills/herdr-team/tests -v
```

在 Herdr 中启动 Lead Claude Code；先试无敏感数据的临时 Workspace：

```text
/herdr-team init dev-review --layout=side --arrange=rows --dry-run
/herdr-team init dev-review --layout=side --arrange=rows
/herdr-team run -- 实现一个最小的无敏感数据样例，审查计划并执行测试
```

底层 `htm.py` CLI 是 Lead 的实际调度工具。`run start` 创建 Run，`run stage` 返回当前角色，`run dispatch` 派单并明确要求 Worker 写入**本次唯一 JSON 文件**，`run gate` 审核，`run finish` 完成。返回 `uncertain` 时，必须通过 `run recover`/`run collect` 核实，不能直接再派。

**典型异常恢复：**

```bash
python3 ~/.agents/skills/herdr-team/scripts/htm.py run list
python3 ~/.agents/skills/herdr-team/scripts/htm.py run recover --run-id RUNID
python3 ~/.agents/skills/herdr-team/scripts/htm.py run collect --run-id RUNID --dispatch-id DISPATCHID
```

Worker JSON 字段和行为说明见 `SKILL.md` 与 `references/operations.md`。

## YAML / JSON 模板配置

`bootstrap-config.sh` 将公开模板复制到 `${XDG_CONFIG_HOME:-~/.config}/herdr-team/team.yaml`，已有文件不覆盖。Runner 优先读取 `--config PATH`，否则使用 `HTM_CONFIG`，再使用用户配置；找不到用户配置时回退到 Skill 自带 `config/team.yaml`。

- `defaults` 定义调度、治理、布局及 Worktree 默认值；`agents` 注册 CLI 类型、Profile 和允许的启动参数；`stages` 定义角色与 Gate。
- 在 `templates` 增加模板；`extends` 继承模板，角色按名称合并，工作流列表整体替换。下面片段要合并到现有 `templates`，不是替换整个配置：

```yaml
# 传统 YAML 写法需要 PyYAML；默认 JSON 格式可表达同样的结构。
custom-review:
  extends: dev-review
  description: 自定义审查模板
  governance:
    report: milestones
  orchestration:
    max_rounds: 3
```

```bash
uv run --extra yaml python scripts/htm.py --config /path/to/team.yaml validate
uv run --extra yaml python scripts/htm.py --config /path/to/team.yaml plan custom-review \
  --set orchestration.max_parallel=2
```

配置只接受声明的字段和枚举；不要把真实 Token、Hermes Profile 内容、个人路径或运行状态写进公开模板。模板扩展示例见 `references/templates.md`。

## 七个模板及权限

| 模板 | Worker | 主流程 |
|---|---|---|
| `dev-lite` | Developer, Reviewer | 简要方案、开发、自测、Code Review、验收 |
| `dev-review` | Developer, Reviewer | Plan、Plan Review、开发、自测、Code Review、验收 |
| `dev-test` | Developer, Tester | 开发、自测、独立测试、验收 |
| `full` | Developer, Tester, Reviewer | Plan Review、开发、自测、独立测试、Code Review、验收 |
| `research` | Researcher, Critic | 调研、独立质疑、Lead 核实与综合 |
| `audit` | Security, Evidence | 安全审计、证据核实、综合 |
| `hermes-guide` | Hermes, Claude Code | Hermes 执行与 Claude 辅助分析、Lead 指导 |

前六种 Worker 当前全是 Claude Code。`hermes-guide` 的 Profile 默认 `work`，首次联调**应替换成专用测试 Profile**，否则可能影响已运行的 Hermes Gateway。模板与覆盖设置详见 `config/team.yaml`、`references/templates.md`。

四项治理参数：`autonomy` (`read-only` / `workspace-write` / `approved-actions`)、`escalate` (`critical` / `important` / `all`)、`approval` (`boundary` / `changes` / `every-step`)、`report` (`final` / `milestones` / `verbose`)；默认 `workspace-write + critical + boundary + final`。

**关键限制：** `read-only` **不保证 Worker 实际只读**，因为 Worker 仍拥有其 OS 用户的访问能力。`--effect` 是声明，不是可靠权限。为防伪造授权，v4.2 将 `run approve` 以及 `run resume --extend-rounds` 设置为明确拒绝；当需要真实授权时会阻塞。要强制只读/审批，须配置真正的 OS/容器隔离和独立用户授权机制，而非仅修改 YAML。`--worktree` 只隔离 Git checkout，不是权限沙盒。

## 质量验收边界

随包 Mock/隔离测试用于证明状态机规则、结果绑定、Git Worktree 与升级回滚脚本的局部性质。**未连接真实 Herdr/Hermes，不等于多 Agent 在真机可持续自主交付**。在真实开发任务上线之前，还需人工完成：Herdr CLI 兼容、真实 Worker 产出 JSON、一次故意 Reviewer 驳回和返工、网络断线恢复、Hermes Profile 专用环境隔离以及外部权限沙盒测试。

## License 与贡献

本项目采用 [MIT License](LICENSE)，Copyright (c) 2026 Shakyae。代码与文档可按 MIT 条款使用、修改及分发，必须保留版权和许可声明。

欢迎通过 GitHub Issue / Pull Request 提交复现步骤、回归测试及文档修正。提交前运行完整测试和配置校验，不提交真实凭证、私有 Profile、日志、缓存或 Run 状态；改变调度、安全边界或协议时必须明确说明兼容性与真实验证范围。

项目未内嵌 Herdr、Claude Code、Codex 或 Hermes 的第三方实现；这些外部工具各自的许可、服务条款和账户费用仍由使用者遵守。可选 PyYAML 为 MIT 许可；安装时由 uv 独立获取，本仓库不分发其源码。

## 官方参考

- https://herdr.dev/tui/docs/cli-reference/
- https://herdr.dev/tui/docs/agent-automation/
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/profile-commands.md
- https://github.com/vercel-labs/skills/blob/main/docs/commands.md
