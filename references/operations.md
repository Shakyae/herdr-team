# v4.2 操作手册与恢复协议

适用 **Herdr TUI**，不是 `hc` App。以实际 `herdr --help` 为准。官方 Skill 请参照 `npx skills add herdrdev/herdr --skill herdr -g`。

## 分工

- 用户（Owner）：提出目标，批准真正跨越边界的动作，查看已核验成果。
- Lead（Claude Code）：分析、规划、审查、纠偏、解释失效情形。**必须保持会话工作到终态；Lead 退出不会让 Python 自动续跑。**
- Python Runner：统一记录 Run、Stage、Dispatch、Worker ID，互斥锁和状态原子保存；调用 Herdr 创建及派发；校验与派单 ID 绑定的 JSON 结果；把不确定状态阻塞。
- Worker：在 Herdr Pane 中实际执行工作，并以派单 ID 提交单独 JSON；报告仍是其自身声明。

## 运行顺序

```bash
# 下列命令示意：在实际 shell 中使用完整 python3 路径
python3 /path/to/herdr-team/scripts/htm.py init dev-review --dry-run
python3 /path/to/herdr-team/scripts/htm.py init dev-review
python3 /path/to/herdr-team/scripts/htm.py run start --goal '具体目标'
python3 /path/to/herdr-team/scripts/htm.py run stage --run-id ID
python3 /path/to/herdr-team/scripts/htm.py run dispatch --run-id ID --role reviewer --message '审查当前阶段' --effect read
python3 /path/to/herdr-team/scripts/htm.py run gate --run-id ID --decision pass --evidence '可核验文件、测试和审查结论'
python3 /path/to/herdr-team/scripts/htm.py run finish --run-id ID
```

`stage actor=lead` 时由 Lead 自己检查证据并记录 Gate。`stage actor=worker` 时由 Runner 先派单，再从 `~/.local/state/herdr-team/artifacts/<RUN_ID>/<DISPATCH_ID>.json` 读取独立 JSON，校验 `run_id`、`dispatch_id`、`role`、`stage`、`stage_attempt`、`status`、`verdict`、`summary`、`evidence` 等字段。没有绑定报告，哪怕 Herdr 显示 `done`，也只能记为 `uncertain`。

`run dispatch --no-wait` 立即记录 `submitted_unsettled`，之后显式 `run collect --run-id ... --dispatch-id ...`；不能仅有一个先前的成功报告就跳过同阶段其他未结算派单。Herdr 的 wait 只代表观察到 Agent 生命周期状态，不保证完成的是哪次任务。

> 关键限制：JSON 内的证据和测试退出码由 Worker 自报。Lead 仍需检查真实 diff/日志/来源；目前没有密码学签名、独立工件签发服务、自动 Git diff SHA 绑定或独立 Supervisor 模型。

## 异常/重连处理

1. 从原来的 Lead Pane 恢复（需要一致的 `HERDR_WORKSPACE_ID/HERDR_PANE_ID/HERDR_TAB_ID`），执行 `run list`；如有未结束 Run，不能启动占用相同 Worker 的新 Run。
2. 对每个未结束 Run 执行 `run recover --run-id ID`，只读返回当前阶段、未结算 Dispatch ID 和结果路径。
3. 核对 Agent/Panes 后，对每个在途或不确定 Dispatch 执行 `run collect ...`。如果结果缺失、身份错误或状态 unknown，继续阻塞，**不自动重复派单**。
4. 如果任务已经被明确放弃，先确保所有 Worker 均不在执行，并通过 `run cancel --run-id ID --confirm-run-id ID --reason '明确放弃原因'` 把运行标记为 `cancelled`；取消不是验收成功。
5. `run pause` 不会杀死 Worker；`run resume` 不会重派 Worker。达到 `max_rounds` 后，由于 v4.2 没有可靠 Owner 授权服务，**暂不支持自动增加轮数**。
6. 初始化因异常失败：`status` 记录 `resources` 和 `pending_operation`，必须人工清点实际 Tab/Pane/Worktree；默认不删除未知资源，也不偷偷重试。

## 复用与 Hermes

同一个 Lead Pane 的 Team 复用前必须保证完整解析配置（含治理、workflow、布局、agent 参数）和 Worktree、repo/task、CWD 完全一致；否则明确拒绝。复用 Agent 必须返回已知 kind 与 Pane ID，不能凭名字或 idle 推断其身份；若 Herdr 返回缺失身份字段则拒绝，待目标版本实测后适配。Agent 的实时 CWD/Profile 不一定能由 Herdr API 返回，**因此复用校验仍不足以证明底层进程已保持原来的安全隔离**。

Hermes 启动允许 `launch_args: []` 或 `['chat']`，Profile 固定由 `-p <profile>` 注入；拒绝混合 `--profile=...` 等重复指定。会检查当前 HTM 状态目录中**其他管理团队**是否占用同 Profile，但不能探测不同 HTM 状态目录、用户或外部 CLI/Gateway 的 Profile 占用。首次联调请使用专用测试 Profile。

## Worktree 与真实文件权限

`--worktree on --repo /absolute/git/root --task lower-case-slug` 在托管目录内创建 Git Worktree。对源 repo 进行只读 `git status --porcelain`，如有已修改或未跟踪文件就拒绝；Worktree 只复制 Git HEAD 可见内容。多个 Worker 共享单个 Worktree，不能当作多写入沙盒。仓库合并、push、删除 Worktree/分支必须单独审批，本 Runner 不会自动执行。

`autonomy` 不是 OS 沙盒，`--effect` 不可信；`approval` 要求授权时，v4.2 的 `run approve` 会**拒绝**而不是接受同一 OS 用户终端输入。对安全敏感代码与真实生产项目，必须另设不同 OS 账户/容器等强制权限边界，不能用这些参数代替。

## 安装与版本

推荐 Skills CLI 进行**全新安装**；官方 Herdr Skill 与本 Skill 分开。`install-local.sh` 是离线安装/升级备份/回滚后备方案；详见压缩包根目录 `README.md`。Python 3.9+，Linux/macOS；真实 Herdr / Hermes 运行兼容性尚需在目标机器验证；Skills CLI 发现与隔离安装不代表真实多 Agent 运行验收。v4.1 运行状态不自动迁移，先清点旧运行再升级。
