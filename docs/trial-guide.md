# HTM 真实任务试用指南

面向准备用真实任务试跑 herdr-team（HTM）的用户。本文只讲「怎么开始、能做什么、哪些做不到」，不重复 `SKILL.md` 的报告契约与 `references/operations.md` 的恢复协议。

试用前提：**独立 Git 仓库或临时 workspace、无敏感数据、有人盯着。** 这不是无人值守的生产工具。

---

## a. 启动与使用

### 前置条件

- Herdr TUI（`herdr`）及官方 `herdr` Skill。
- 已安装并认证的 Claude Code CLI（Worker 默认全是 Claude Code）。
- Python 3.9+；源码开发与可选依赖用 [uv](https://docs.astral.sh/uv/) 管理，不要往系统 Python 装依赖。
- `hermes-guide` 模板另需 Hermes CLI 和**专用测试 Profile**，其他模板不需要。

### 安装与验证

全新安装（在线，Skills CLI 方式，锁定试用版 `v4.2.0-beta.2`）：

```bash
npx skills add herdrdev/herdr --skill herdr -g
npx skills add https://github.com/Shakyae/herdr-team/tree/v4.2.0-beta.2 \
  --skill herdr-team \
  -g -a claude-code -a codex -y
bash ~/.agents/skills/herdr-team/scripts/bootstrap-config.sh
```

从本地源码离线安装（不使用 Skills CLI）：

```bash
bash install-local.sh
ls -ld ~/.agents/skills/herdr-team ~/.claude/skills/herdr-team ~/.codex/skills/herdr-team
```

安装后验证：

```bash
uv run --project ~/.agents/skills/herdr-team --extra yaml python ~/.agents/skills/herdr-team/scripts/htm.py validate
uv run --project ~/.agents/skills/herdr-team --extra yaml python ~/.agents/skills/herdr-team/scripts/htm.py templates
```

在仓库根目录验证源码（未安装也能跑）：

```bash
uv sync --extra yaml
uv run --extra yaml python scripts/htm.py validate
uv run --extra yaml python scripts/htm.py init dev-review --layout side --dry-run
```

`validate` 返回 `"valid": true` 且 `templates` 列表包含七个模板，就说明配置可解析。

### 在 Herdr 中起一个 Run

先在 Herdr 里启动 Lead Claude Code，然后：

```text
/herdr-team init dev-review --layout=side --arrange=rows --dry-run
/herdr-team init dev-review --layout=side --arrange=rows
/herdr-team run -- 实现一个最小的无敏感数据样例，审查计划并执行测试
```

`init` 会真实创建 Tab/Pane 和 Worker；先 `--dry-run` 看一遍计划。

底层 CLI 最小闭环（`scripts/htm.py` 是 Lead 实际的调度工具，路径按实际安装位置替换）：

```bash
python3 /path/to/herdr-team/scripts/htm.py run list
python3 /path/to/herdr-team/scripts/htm.py run start --goal '具体目标与验收标准'
python3 /path/to/herdr-team/scripts/htm.py run drive --run-id <RUN_ID>
```

中断后用 `run drive --resume` 接管，不要重开 Run：

```bash
python3 /path/to/herdr-team/scripts/htm.py run drive --run-id <RUN_ID> --resume
```

也可以完全手动推进：

```bash
python3 /path/to/herdr-team/scripts/htm.py run stage --run-id <RUN_ID>
python3 /path/to/herdr-team/scripts/htm.py run dispatch --run-id <RUN_ID> --role developer --effect write --message '明确任务、边界及验收方式'
python3 /path/to/herdr-team/scripts/htm.py run collect --run-id <RUN_ID> --dispatch-id <DISPATCH_ID>
python3 /path/to/herdr-team/scripts/htm.py run gate --run-id <RUN_ID> --decision pass --evidence '基于可核验资料的具体审核结论'
python3 /path/to/herdr-team/scripts/htm.py run finish --run-id <RUN_ID>
```

只有 `run finish` 返回 `finished=true` 才算流程走完。

---

## b. 现有能力

- **七个模板**：`dev-lite`、`dev-review`、`dev-test`、`full`、`research`、`audit`、`hermes-guide`，用 `templates` 子命令列出实际角色与 workflow。前六种的 Worker 当前都是 Claude Code。
- **Run/Dispatch 账本**：`run start` 建 Run，每次派单一个 Dispatch ID，状态持久化在 `~/.local/state/herdr-team/`，进程锁保证跨 Run 的 Worker 互斥（未结束 Run 会一直预约它的 Worker）。
- **JSON 工件绑定校验**：Worker 必须在调度器给的唯一路径写本次派单的 JSON，`run_id` / `dispatch_id` / `role` / `stage` / `stage_attempt` 任一不符、文件缺失或被改动，都不通过 Gate。终端里显示 `done` 不算证据。
- **跨 Run Worker 互斥**：同一 Pane 不能被两个未结束 Run 同时占用；`run start` 和 `adopt` 会拒绝已被未结束 Run 预约的 Worker。
- **`run drive` 前台串行协调**：按账本推进阶段，自动回收未结算派单、按报告驱动 Worker gate、按 verdict 自动 `gate revise` 并派发 `--remediation`；到 Lead 阶段停止，不会替你 finish/审批/提交/清理。默认 20 步、1800 秒。
- **显式放弃与只读恢复**：`run cancel --run-id ID --confirm-run-id ID --reason '...'` 明确标记放弃（不是成功）；`run list` / `run recover` 只读恢复视图，不自动重发。
- **`run collect` 不重发**：根据绑定的 JSON 和已知 Agent 状态结算；报告缺失或无效时通常为 `uncertain`，Agent 为 `blocked` 时可为 `blocked`；Agent 身份无法核验时会报错。

真实联调已验证的两条：真实 Reviewer 判 `needs-work` 后 drive 自动 `gate revise` + `--remediation`，修复后 reviewer 角色的第二轮复审确认 `pass`；以及 SIGKILL 杀 driver 后用 `--resume` 收回到真实产物。范围和局限见 v4.2.0-beta.2 的 CHANGELOG。

---

## c. 限制（试用前必读）

**Happier 兼容是硬限制。** 经 Happier 包装启动的 Claude Code 不上报 `agent_session`，由此：

- `name-lead` 会拒绝执行（无法验证 session 身份）；
- `adopt` 无法绑定被接管的 Worker（无法验证 `agent_session`）；
- `run drive` 自动模式要求所有 Worker 都有可验证 `agent_session`，否则返回 `identity_unbound`。

结论：**Happier 身份兼容未实现，本次试用请让 Lead 与 Worker 都运行在原生 Herdr 环境。** 具体地：`name-lead` 在 Happier 包装的 Lead 上不可用；`adopt` 无法接管 `agent_session` 不可核验的 Worker；`run drive` 的身份校验只覆盖 Worker（`run["roles"]`）的 `agent_session`，不校验 Lead，未绑定的 Worker 会让它返回 `identity_unbound` 并改用 Lead 手动命令（`run dispatch` / `run collect` / `run gate`）。这也是为什么本次真实联调全部在原生 Herdr 的 `wF` 中完成。这是环境限制，不是缺陷；不要伪造身份绕过。

**已有 Worker 接管（`adopt`）条件很严。** 它只注册**全新** team——该 Lead Pane 已有注册文件就直接拒绝（`path.exists()`）；必须传全部角色的 `role=agent-name`；每个 Worker 要在 `idle`/`done`、kind 可验证、同 workspace、非 Lead pane、cwd 一致、`agent_session` 可验证绑定；任何一条不满足即拒绝。Worker 被未结束 Run 预约、或已被其他 team 注册时也拒绝。

**本次 SIGKILL 只针对 driver 进程，未杀 Worker。** 实测 Worker 在 driver 死后继续工作并写出报告（artifact mtime 比杀点晚 80.5 秒）。所以「恢复」能收回到真实产物，但**不能保证崩溃前已完成的工作被保住**。第一次 `--resume` 若撞上 Worker 仍在 `working`，collect 会返回 `uncertain` 并 `stuck_unsettled` 退出——这是 fail-closed 设计。若 Worker 仍在 `working`，可等它完成后重试 collect；若持续 `uncertain`、Worker `blocked` 或身份无法核验，应先检查状态与工件，不要盲目重发。

**`run drive` 是前台循环，不是后台服务。** Lead 会话退出或终端断开，不会自动续跑；没有守护进程、没有被动的 Worker 状态轮询服务。

**`run approve` 会拒绝而不是接受审批。** v4.2 没有 Owner 授权服务，同 OS 用户的终端输入不能证明身份，因此 `run approve` 和 `--extend-rounds` 直接拒绝，需要审批的任务会被挡住（fail-closed），而不是假装通过。

**Worker 报告是自报。** JSON 只是身份绑定的自我陈述，不是独立证明。Lead 必须自己看 diff、日志和测试输出；`tests[].exit_code=0` 也不等于测试真的有效。

**`--effect` 不可信，`autonomy` 不是 OS 沙盒。** `read-only` 只检查派单声明的 effect，不阻止 Worker 写文件；真正只读要靠独立 OS 用户、容器或工具权限。生产仓库、敏感数据必须另设权限边界，不能靠 YAML 参数。

**未验证边界**：external exactly-once 投递、OS sandbox 隔离、任意崩溃时刻的原子性。Hermes 模板也尚未做真实联调。

---

## d. 试用建议

- 从**无敏感数据的独立 Git 仓库或临时 workspace** 开始，不要用生产仓库、私人原始数据或密钥目录。
- 每个 Run 前先 `run list`，确认没有未结束 Run 占着你要用的 Worker；跨 Run 互斥不会自动清理。
- 先跑一次 `init ... --dry-run`，确认 Pane/Tab/Worktree 计划与你预期一致再真正 `init`。
- 出现 `uncertain` / `submitted_unsettled` 一律先 `run collect`，**绝不重发**；重发会导致同一个 Agent 收到两份冲突任务。
- 明确放弃时用 `run cancel`：需要 `--confirm-run-id` 与 Run ID 匹配、`--reason` 至少 12 字符、本 Run 无未解决 dispatch、且各 Worker 处于可安全释放的状态；不要把取消当验收成功。
- Lead 全程盯着 `run drive`；它到 Lead 阶段就会停，后面的验收、finish、提交需要你自己做。
