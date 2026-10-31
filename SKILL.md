---
name: herdr-team
description: 在 Herdr TUI 中管理配置化多 Agent 团队，通过可追踪派单报告、状态账本和必要的独立审查辅助 Lead 协调任务。显式使用 /herdr-team。
argument-hint: "init <template> [--dry-run] | run -- <goal> | status | templates | pause | resume"
disable-model-invocation: true
---

# Herdr Team Manager v4.2.0-beta.1 — Lead 操作协议

> 适用 **Herdr TUI（`herdr`）**，Lead 当前优先 Claude Code。请先阅读本 Skill 的 `references/operations.md` 及已安装的 Herdr 官方 Skill。`$ARGUMENTS` 是用户原始指令。**Skill 不是一个独立运行的后台 Supervisor，也不构成操作系统安全沙盒。**

## 不变量：不能提前汇报“完成”

1. Lead 必须先调用 `run start` 获取 Run ID，再按 `run stage` 所列阶段逐一工作。每次派单都保留 Run ID / Dispatch ID；**不允许跳过 Reviewer、Tester、验收关卡**。
2. Worker 必须在指定的结果路径提供本次派单独有的 JSON 报告，不能仅回复“done”，不能把 `herdr agent read` 的终端旧文字当作证据。`run dispatch` 默认等待；若返回 `uncertain`，先检查并执行 `run collect`，**不要擅自重发**。
3. `run gate pass` 要求本阶段全部派单均已成功结算，目标角色最后一次报告的 `verdict=pass`，自测/独立测试须有 `exit_code=0` 的测试记录。Lead 还必须判断证据真伪、审查实际文件和 diff；结构化报告是**身份绑定的自我陈述，不是独立证明**。
4. 只有 `run finish` 返回 `finished=true` 才能向用户宣称该 Run 的流程已完成。流程完成 **不自动等于成果质量合格**，最终结论需列出已测/未测。若 Lead 被中断，再次启动后先执行 `run list` / `run recover` 续办，不能把未完成任务报成成功。
5. 无法安全推进则报告 `blocked/uncertain`，需要人作出决策时停止，不伪造授权、不跳过关卡。所有七种模板都采用相同自主监督原则；模板只定义 Worker 与工作流组合。

## 常用操作（在 Lead 的工具终端执行）

Skill 的 `scripts/htm.py` 是唯一调度 CLI。请定位本 Skill 实际路径，不要假设 `$PWD` 在 Skill 目录。

```bash
python3 /path/to/herdr-team/scripts/htm.py templates
python3 /path/to/herdr-team/scripts/htm.py validate
python3 /path/to/herdr-team/scripts/htm.py plan dev-review --layout side
python3 /path/to/herdr-team/scripts/htm.py init dev-review --layout side --dry-run
python3 /path/to/herdr-team/scripts/htm.py init dev-review --layout side
python3 /path/to/herdr-team/scripts/htm.py status
python3 /path/to/herdr-team/scripts/htm.py run list
python3 /path/to/herdr-team/scripts/htm.py run start --goal '具体目标与验收标准'
python3 /path/to/herdr-team/scripts/htm.py run stage --run-id <RUN_ID>
python3 /path/to/herdr-team/scripts/htm.py run drive --run-id <RUN_ID> [--resume] [--max-steps 20] [--deadline-seconds 1800]
python3 /path/to/herdr-team/scripts/htm.py run dispatch --run-id <RUN_ID> --role developer --effect write --message '明确任务、边界及验收方式'
python3 /path/to/herdr-team/scripts/htm.py run collect --run-id <RUN_ID> --dispatch-id <DISPATCH_ID>
python3 /path/to/herdr-team/scripts/htm.py run gate --run-id <RUN_ID> --decision pass --evidence '基于可核验资料的具体审核结论'
python3 /path/to/herdr-team/scripts/htm.py run finish --run-id <RUN_ID>
```

- 新建 Worker 使用真实 Pane ID 派生名称；`name-lead` 默认预览（无 team 亦可），只有显式 `--apply` 才重命名当前 Lead Agent；用 `--expect-session <预览的 session value>` 核对跨命令身份，不会自动重命名已有 Agent。
- `name-lead` 要求 Lead 的 Agent 具备可验证的稳定 session 身份（`agent_session.value`），`--apply` 必须传入预览输出的 `--expect-session`。经 Happier 包装启动的 Claude Code 目前不上报 `agent_session`，因此该环境下 `name-lead` 会拒绝执行——这是环境限制，不是功能缺陷；不要用伪造身份或跳过校验的方式绕过。
- `run drive` 是前台串行协调循环，并非无人值守服务：基于账本回收未结算派单、按报告驱动 Worker gate，在 Lead 阶段停止；不会 finish、审批、提交或清理。`--resume` 用于接管意外中断的 drive；每次行动前以账本为准，绝不因进度提示或文件缺失重发。不支持自动返工的 verdict、审批及异常需人工处理。默认最多 20 步、1800 秒；每次派单前按剩余期限收紧 Herdr 与 subprocess 等待，剩余不超过 5 秒不再派单。超时只回收、不重发，返回 `deadline_exceeded` 标明是否已越过期限。
- 自动 drive 要求所有 Worker 绑定可验证 `agent_session`；旧 team 未绑定时需重建或 `adopt`，手动命令仍可使用。自动 gate 仅依据 Worker 自报及其工件哈希，不是独立验证；Lead 必须在验收前核对文件、测试和证据。
- `run gate --decision revise`：Worker 反馈不通过，进入新一轮，必要时先派 Developer `--remediation`，然后重新让 Reviewer/Tester 检查。
- `run collect`：**只**根据任务专属 JSON + 已知 Agent 状态完成结算，终端快照不算证据。
- `run pause/resume`：管理已保存的执行状态；不会自动启动或杀死 Worker。
- `run list/recover`：断线重连时只读列出未结束 Run 和待回收派单，不自动重发。
- `run cancel --run-id ID --confirm-run-id ID --reason '...'`：明确放弃且所有 Worker 已停止工作时释放 Team，**不是成功完成**。

## Worker 报告契约（由调度器在派单时自动传给 Worker）

请每次在调度器提供的**唯一**路径保存 UTF-8 JSON，且**通过同目录临时文件 + 原子重命名**写入。示例仅供理解；ID 和路径必须以实际 `run dispatch` 给定值为准：

```json
{
  "run_id": "a1b2c3d4e5f6",
  "dispatch_id": "0123456789",
  "role": "developer",
  "stage": "self_test",
  "stage_attempt": 1,
  "status": "complete",
  "verdict": "pass",
  "summary": "已完成指定修改，并记录实际验证范围与未验证的限制。",
  "evidence": ["实际文件路径及变更摘要", "已执行的测试和对应输出"],
  "tests": [{"command": "python3 -m unittest", "exit_code": 0}]
}
```

字段不匹配、旧报告、缺文件、未知状态、报告被修改等均不得通过 Gate。报告所列测试结果仍需 Lead 核对真实日志。

## 安全、审批和行为边界

- `autonomy` 的 `read-only` 只**检查派单声明的 effect**，不防止 Worker 擅自写文件；真实安全只能由不同 OS 用户、容器/沙盒、工具权限实现。默认 `workspace-write` 不等于获得任何敏感目录访问授权。严禁在未隔离的 Worker 中处理 私人原始数据、密钥、生产仓库等敏感数据。
- **v4.2 暂停同用户 TTY `run approve` 和 `--extend-rounds` 功能**：此前方式不能验证真实 Owner 身份。需要变更/每步审批的运行会被挡住，而不是以假安全确认继续。先由人工使用独立外部审批/沙盒流程安排需要授权的任务。
- `escalate` 与 `report` 仅供 Lead 按用户要求控制汇报频率，**没有系统级通知守护进程**。
- Hermes Profile 必须显式配置并通过 `hermes profile show`；调度器可发现其他 HTM Team 对同 Profile 的占用，**不能发现外部 Hermes CLI/Gateway 会话**。只用独立测试 Profile 做首次联调。
- Worktree 按任务创建，**不是**每 Worker 一份仓库。默认创建前要求源 Git 仓库无未提交/未跟踪内容；不复制 Raw，不自动合并/清理。
- Python 只提供确定性阶段管理、互斥、状态保存、命令派发和自报工件校验。**不会**自行推理、复检任务真实性或在 Lead 退出后自主继续执行。
- v4.1 的 Team/Run 持久化状态不自动迁移到 v4.2。升级前先清点未完成 Run；切勿在尚有在途派单时简单复用旧 Pane。
- 不引入 DSH、Compound、Recall 或 AGENTS.md 自动工程记忆。

真实环境仍未联调；先在专用无敏感数据 Workspace 做 dry-run、Mock 通过后的真机验证。具体见 `references/operations.md`。
