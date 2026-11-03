# Happier 内 `/herdr-team init` 的最小可实施修复路径

- 状态：设计文档（未改正式代码、未提交、未发版）
- 证据标签：**[实测]** 本机真实执行并核对；**[源码]** 从已安装产物读到；**[推断]** 基于前两者的推理，未端到端验证
- 版本：Herdr 0.9.5（protocol 22）、Happier 0.2.17、HTM v4.2

---

## 1. 结论

**采用方案 B：HTM 用内核父链 + `pane.process-info` 自证所在 Pane。** 改动只落在 `scripts/htm.py`，不依赖上游发版。

方案 A（Happier 代注册 Session）**不能单独修复本问题**：`agent_session` 挂在 Pane 上，HTM 必须先知道自己属于哪个 Pane 才能读它，属循环依赖。A 只能作为后续阶段解决 `name-lead`/`adopt`，且被一个未解问题阻塞（§7）。

---

## 2. 根因（三层）

| 层 | 事实 | 证据 |
|---|---|---|
| 环境 | Happier 启动的 claude 子进程（pid 29187）环境中没有 `HERDR_ENV`/`HERDR_PANE_ID`/`HERDR_WORKSPACE_ID`/`HERDR_TAB_ID`/`HERDR_SOCKET_PATH`，也没有 `CLAUDE_CODE_SESSION_ID` | **[实测]** `ps eww` 逐键核对 |
| 代码 | Happier 两处白名单丢弃 `HERDR_*`：`stripNestedSessionDetectionEnv` 显式 `delete HERDR_ENV`；`buildClaudeSubprocessEnv` 的 exact/prefix 白名单不含 `HERDR_` 前缀；统一终端路径另用 `buildTerminalLauncherProcessEnv` | **[源码]** `api-BJP7Fl6P.mjs:95121`、`:95566`；`command-1D3NR-aN.mjs:21362` |
| HTM | `context()` 读 `HERDR_ENV` 与三个 ID，缺失即抛 `Must run inside Herdr: HERDR_ENV=1`；`--dry-run` 不调 `context()` 所以能过 | **[实测]** 移除 `HERDR_*` 后 `status` rc=2；**[源码]** `scripts/htm.py:247-265` |

**关键事实**：`HERDR_*` 只在 claude 子进程层丢失。Happier 自己的 runner（pid 29054）环境中 `HERDR_ENV=1`、`HERDR_PANE_ID=wH:p5`、`HERDR_TAB_ID`、`HERDR_WORKSPACE_ID`、`HERDR_SOCKET_PATH` 全在。**[实测]** 因此 Pane 归属在 Happier 侧是确定的，只是没有合法通道传给 HTM。

---

## 3. 三条候选路径

| 路径 | 能解决 | 不能解决 | 判定 |
|---|---|---|---|
| A. Happier 调 `pane.report_agent_session` | Session 身份（`agent_session`） | Pane/Workspace 定位；`context()` 仍失败 | **不能单独用** |
| B. HTM 用父链 + `process-info` 自证 | Pane/Workspace/Tab 定位；`init`/`status`/`run drive` 前置 | Session 身份（Happier 的 claude 本就不上报） | **本次采用** |
| C. Herdr 服务端提供可信调用者身份 | 两者都能 | 需核心改动 + 协议变更 + 发版 | 长期正解，本次不动 |

Herdr 0.9.5 **没有**可信调用者身份接口：**[实测]** `herdr api schema`（protocol 22）无 caller/peer/client_id 等字段；`pane.current` 的 `caller_pane_id` 由调用方自报，服务端不校验；`pane.current` 在无 `HERDR_*` 时返回**焦点** Pane（当时 `wH:p7`，而调用者在 `wS:p2`），与调用者无关。

---

## 4. 方案 B 的实现

全部改动在 `scripts/htm.py`。

### 4.1 新增 `pane_of_current_process()`

位置：`context()` 之后（约 267 行）。职责：**不读不写任何 `HERDR_*`**，返回 `{"session", "workspace", "tab", "pane"}`。

1. `herdr session list --json` → 取 `running: true` 的 session。**[实测]** 当前返回 `[{name:"default", running:true, socket_path:"..."}]`。
2. 对每个 session 执行 `herdr --session <name> pane list`。**[实测]** 无 `HERDR_*` 环境下 rc=0。
3. 祖先集合：从 `os.getpid()` 起循环 `/bin/ps -o ppid= -p <pid>` 直到 `ppid <= 1` 或 64 层上限。用绝对路径是**防御性写法**：**[实测]** Happier claude 的 PATH 实际含 `/usr/bin`、`/bin`、`/usr/sbin`、`/sbin`，裸 `ps` 也可用，但不应依赖调用方 PATH 的解析顺序。
4. 对每个 session 的每个 Pane 执行 `pane process-info`，取 `shell_pid` 与 `foreground_processes[].pid` 的并集，与祖先集合求交。
5. **唯一命中**才返回，并带上命中所属的 session 名；0 命中或多命中一律 `raise Error`，错误分两种文案（"无法定位" / "身份不唯一，拒绝猜测"）。多命中即使同属一个 session 也拒绝。
6. 返回前复用现有 ID 正则与"tab/pane 前缀必须等于 workspace"校验（与 `context()` 同构）。

可靠性依据：`ppid` 由内核在 fork 时写入，进程无法自报；`process-info` 的 pid 来自 Herdr 服务端自己的 PTY 记录。两侧同时命中才成立。

**[实测]** 本进程唯一命中 `wS:p2`；Happier 的 claude（29187）与 MCP 桥（29222）为种子均唯一命中 `wH:p5`；原生 claude（93044）唯一命中 `wS:p3`。11 个 Pane 全量扫描耗时 0.065s。

### 4.2 修改 `context(require=True)`（247-265）

**分支顺序与现有代码一致，只在"完全没有 `HERDR_ENV`"时新增一条兜底：**

| 情形 | 行为 | 二次探测 | 理由 |
|---|---|---|---|
| `HERDR_ENV == "1"` 且三个 ID 齐全 | 完全走现有逻辑，零变化 | **不做** | 原生可靠性不降级；不涉及 `process-info`，也就不存在需要二次校验的探测 |
| `HERDR_ENV == "1"` 但 ID 缺失/非法 | **保持现有报错**（`Missing Herdr pane/workspace/tab environment` 等），**不退回派生** | 不适用 | 现有 `scripts/htm.py:253-254` 已这样处理，`tests/test_regressions_v42.py:270-276` 断言该文案。在"已声明在 Herdr 内但环境不完整"时改用另一套身份来源，会掩盖真实的环境错误 |
| 无 `HERDR_ENV` | 调 `pane_of_current_process()` | **做** | 这是 Happier 的情形，也是唯一需要二次校验的分支 |

**只有派生分支做二次全量交集。** 原生分支既不探测也不校验，session 取空串、`herdr()` 走默认解析，行为与今天完全一致。因此"原生分支 session 为空却要与非空值比较"这一矛盾不存在。

**`require=False` 契约保持不变**：返回 `{"workspace","lead_tab","lead_pane"}` 三键，提前返回、不解析 session、不调 herdr。`tests/test_regressions_v42.py:290-294`（两条断言在 :292 与 :294）无需修改。

**错误文案**：唯一需要改的既有断言是 `tests/test_regressions_v42.py:277-278`，建议改为匹配 `^Must run inside a Herdr pane: HERDR_ENV=1 is absent ...`。该用例在 `clear=True` 下运行，新路径会真的去调 herdr，因此必须注入 fake。

**fake 兼容性（可核验）**：`tests/test_htm.py:206` 的 `fake_herdr(*argv, **kwargs)` 支持新 keyword；`tests/test_regressions_v42.py` 的 `with_session`/`fake`/`racing`/`busy_twice`/`blocked`/`unknown` 六个 fake 均为 `*args, **kwargs` 或 `*argv, **kw` 形式，同样兼容。新增 `session=` keyword 不会破坏它们。其余 fake（`tests/test_drive.py` 与 `test_regressions_v42.py` 内更晚定义的同名 `fake`）在实现时需逐一确认。

**不要**把解析结果写回 `os.environ`：那会制造一个可被后续进程继承的伪造源。

### 4.3 session 维度

`context()` **永远不包含 session**。原因：它的返回值被 `scripts/htm.py` 705/869/986 原样落盘，被 904/943/964 精确相等比较。任何新增顶层键都会让全部既有账本失效——**[实测]** 现有 3 个 team 与 6 个 run 的 `context` 均为恰好 3 键，其中 wS 的 `95fd9bc7acd24c75` 正被 Run `bdd4938067b5` 使用。

改为平行返回：

```python
def lead_identity(require=True) -> dict:
    """{"context": {"workspace","lead_tab","lead_pane"}, "session": str}"""
```

`context()` 成为 `lead_identity()["context"]` 的薄封装，因此**所有既有落盘与 `!=` 比较零改动，三个既有 team 与六个 run 都不需要迁移**。`state_file` 的 key 仍是 `sha256("workspace|lead_pane")[:16]`（269-271），不分裂。

**session 取值规则**

| 来源 | 取值 | 额外调用 |
|---|---|---|
| 有 `HERDR_SOCKET_PATH` | 在 `herdr session list --json` 的 `sessions[].socket_path` 中反查名字 | 是（仅此一路） |
| 无 `HERDR_SOCKET_PATH` | `""`（空串＝交给 `herdr()` 默认解析，等价于现状） | 否 |
| 派生路径 | 命中所属 session 名，必然非空 | 已含在探测中 |

**fail closed 规则（不得静默选 default）**：
- `herdr session list --json` **命令本身失败**（非零退出、超时、输出不是合法 JSON）→ 直接 `raise Error`。`scripts/htm.py:314-339` 的 `process()` 已对这三类情况抛 `Error`/`HerdrError`，**不得捕获后降级为"找不到 session 所以跳过"**——那会把环境故障伪装成"无身份"，从而放行。
- `session list` 成功但返回 0 个 running session → `raise Error`。
- 反查 socket 匹配 **0 个或 >1 个** → `raise Error`。
- 派生路径下多 session 且出现同名 Pane → `raise Error`（冲突存在时不做猜测）。

**为什么不用 `HERDR_SESSION_NAME`**：**[实测]** Herdr 注入的 `HERDR_*` 只有 6 个（`HERDR_BIN_PATH`/`ENV`/`PANE_ID`/`SOCKET_PATH`/`TAB_ID`/`WORKSPACE_ID`），无此变量。

**`herdr()` 新增可选参数**：

```python
def herdr(*args, timeout=40, session=None):
    # 空串/纯空白视同 None；非法字符集直接报错
    argv = ["herdr"] + (["--session", session] if session else []) + list(args)
```

必须用**非空真值**判断：**[实测]** `herdr --session "" pane list` → `session name cannot be empty`；`herdr --session " " pane list` → `session name may only contain ASCII letters, numbers, '.', '_' and '-'`。默认 `session=None` 时拼出的 argv 与现在逐字节相同，15 处既有调用全部不受影响。session 名以 `herdr session list --json` 返回值为准，正则 `[A-Za-z0-9._-]+` 只是把 CLI 自身规则前置成 `Error`。

**需要透传 session 的位置**（6 个既有 `context()` 调用点之外还有间接链）：

| 位置 | 用途 | 需 session |
|---|---|---|
| 665 `init` | 722/734 `tab create`/`pane split`、348/380/386 探活与启动、438/464 `lookup_agent`/`agent get` | 是 |
| 771 `adopt` | 846 `pane get`、438 `lookup_agent` | 是 |
| 901 `name_lead` | 908 `pane get`、912/935 `agent get`、947 `agent rename`、454 `require_name_available` | 是 |
| 974 `run_start`（间接） | 经 `current_team()` 拿到 ctx，974 `verify_existing` → 438 | 是 |
| 1117/1120 `run_dispatch`（间接） | `verify_existing` 与直接 `lookup_agent` | 是 |
| 1174 `run_dispatch` 发 prompt | `herdr("agent","prompt",...)` | 是 |
| 1295-1309 `run_drive.check_identity` | 内部经 `verify_existing`/`lookup_agent` 读 Agent，不自己拼命令 | 是（作为 `check_identity(role, session)` 透传） |
| 890 `current_team`、963 `require_run`、1563 `run_list` | 只读账本 | 否 |

`run_start`/`run_dispatch` 拿不到 `context()` 的返回值，因此 `current_team()`/`require_run()` 需另出返回 session 的内部变体。**不得用模块级全局变量传 session**（会跨调用泄漏）。

### 4.4 阶段序列与二次校验

**二次校验只适用于派生分支**（原生分支不做，见 §4.2）。序列为：

| 阶段 | 动作 | 失败时 |
|---|---|---|
| S1 | `context()`/`lead_identity()` 执行**第 1 轮**探测 | 报错，不返回部分结果 |
| S2 | **第 2 轮完整探测**，要求与 S1 逐项相同（`session`/`workspace`/`tab`/`pane`） | **报错，此时尚未产生任何副作用** |
| S3 | `init` 现有流程（创建 partial 账本、`git worktree add`、首个 Herdr 写操作） | 现有逻辑 |

**关键：第 2 轮必须在 `scripts/htm.py:707-708` 的 `save(path, team)`（首次落盘 partial 账本）之前完成。** 原因是可以避免一个无法恢复的状态：

- `init` 在 `:707-708` 先写 `status: "partial"` 账本，随后 `:718-719` 可能创建 worktree，再在 `:720+` 写 Herdr；
- 而 `:680-681` 对已存在的 partial 账本**直接拒绝重试**（`Previous init is partial...`），没有自动恢复路径；
- 因此若二次校验放在落盘之后失败，会留下"partial 账本 + 可能已建的 worktree + 零个 Pane"，用户只能人工清理。这是本轮要避免的。

放在落盘之前则失败时无任何残留：不写账本、不建 worktree、不动 Herdr，用户直接重试即可。

**代价**：第 2 轮与首个 Herdr 写操作之间隔了 `git worktree add`（可能秒级），窗口比"紧贴写操作"更大。这是有意取舍——**用更大的窗口换"失败零残留"**。若 Owner 更看重窗口大小，替代方案是保留原位但**必须额外定义 partial 的可恢复语义**（例如记录 `phase` 字段并在下次 init 时按 phase 清理），属未设计的扩展，本轮不采纳。

至此 S3 内部的首个写操作**依配置而变**：`layout.mode == "tab"` 时是 `tab create`（`scripts/htm.py:721-723`），否则是 `pane split`（`:726-728`）。

### 4.5 竞争窗口（如实表述）

两轮完整探测**只能降低误判概率，不能证明零窗口**：

1. **轮内变化**：两轮不一致时 fail closed，这部分被覆盖。
2. **轮后窗口**：第 2 轮探测结束到首个 Herdr 写操作之间仍有一段窗口。按 §4.4 的顺序，这段窗口**包含** `git worktree add`（可能秒级），因此比"紧贴写操作"的方案更长——这是为换取"失败零残留"而有意接受的代价。若该窗口内 Pane 前台进程被替换——包括 ABA 型（换走又换回、或换成同样落在祖先链上的兄弟进程）——第 2 轮的结论就过期了，而 HTM 无法再观察。**这段窗口不可关闭**：不存在能同时锁住 Pane 前台进程与写 Herdr 结构的原子操作。

落地只有两个选择：

| 选择 | 做法 | 局限 |
|---|---|---|
| 接受 | 明写风险，不做额外防护 | ABA 场景下 `init` 会**成功写入错误对象**而无法察觉；`partial`/`resources` 只在写入失败时有帮助 |
| 写入前终态复核 | 每个 `operate(...)` 前都做一次完整探测 | 复核与实际 `herdr` 子进程执行之间仍有微秒级窗口，不可关死；`init` 有 3-4 次 split/start，耗时显著增加 |

---

## 5. 安全性

### 5.1 不可接受的冒充手段

| 手段 | 为什么不行 |
|---|---|
| 手设 `HERDR_ENV=1` / `HERDR_PANE_ID=...` | ID 可任意自报，身份校验退化为字符串比较 |
| 转发 Happier runner（29054）的 `HERDR_*` | 那是 Happier 的身份，不是调用者的；任何子进程都能继承 |
| 用 `pane.current` 推断 | **[实测]** 返回焦点 Pane，与调用者无关 |
| 自报 `caller_pane_id` | 服务端不校验对端 |
| 以 `source="herdr:claude"` 调 `report_agent_session` | 冒用官方集成事源 |
| 用 `HAPPIER_SESSION_ID`/`TERM_PROGRAM=herdr` 当身份 | 前者与 Pane 非一一对应，后者是常量 |
| 仅凭 pgid 判定归属 | pgid 可被 `setpgid` 改写，且 Pane 内多进程共享 |

### 5.2 本方案的边界

- 证明的是"**我在这个 Pane 里**"，不是"我是这个 Pane 的 Lead Agent"。同 Pane 内的兄弟进程同样命中，这是正确语义而非漏洞。
- 不回答"我是否有权操作这里"。`verify_existing`/`check_identity` 的语义保持不变，不得因 B 而放宽。
- 结果不落盘、不写回环境、不继承。

---

## 6. 验证（隔离 Workspace）

**V0** `uv run pytest -q` → 基线 `204 passed, 116 subtests passed`（**[实测]**）。实现后只应增加。

**V1** 复现：`env -u HERDR_ENV -u HERDR_PANE_ID -u HERDR_WORKSPACE_ID -u HERDR_TAB_ID -u HERDR_SOCKET_PATH python3 scripts/htm.py status` → rc=2，stderr 为 `Must run inside Herdr: HERDR_ENV=1`（**[实测]**）。

**V2** `herdr workspace create --label htm-once-<slug>`，记录返回的 workspace id；在其中用 `happier claude` 启动 Lead。`init dev-lite --dry-run` 应返回 `requires_herdr: true`、`no_mutations: true`。

**V3**（核心）在该终端执行真实 `init dev-lite --task <slug>`，判定全部成立：
- rc=0 且 `reused: false`；
- 新 Pane 的 `workspace_id` 等于 V2 的 workspace；
- 同一终端内 `env -u HERDR_ENV ... python3 scripts/htm.py status` 返回 rc=0（直接证明 B 生效且不依赖环境变量）；
- 新建 team 文件的 `context.workspace` 等于 V2 workspace。

**V4**（负例，必须与 V3 同时成立）：
- 在非 Herdr 终端执行同一命令 → rc=2，且文案说明"进程树未唯一定位到 Herdr Pane"；
- 单测（派生分支）：0 命中抛错、多命中抛错、两轮不一致抛错；
- 单测（原生分支）：`HERDR_ENV=1` 且 ID 合法时**不**触发 `process-info`，且**不做**第二次探测（断言调用序列中无 `process-info`、无第二次 `pane list`）；
- 单测：第 2 轮失败时**未产生任何副作用**（无 team 账本、无 worktree、无 Herdr 写入）。

**V5** 清理：确认无未结算 Run 后 `herdr workspace close <V2 id>`。不要删除本地账本，不要触碰 `wH`/`wS`。

判定标准一句话：**移除全部 `HERDR_*` 后 `init` 与 `status` 仍成功，同时非 Pane 环境下严格失败。两条必须同时成立。**

---

## 7. 剩余障碍

**已解决（B 落地后）**：真实 `init`、`status`、`run start`/`run list`、`run drive` 的 Lead 侧上下文；Workspace/Pane 归属不再依赖环境变量。

**仍需上游（Happier）**：`name-lead` 与 `adopt` 在 Happier 上仍不可用——Happier 的 claude 不上报 `agent_session`（**[实测]** `wH:p5` 为 `null`）。

**Open Question（阻塞 A 落地，未实测）**：Happier 上报后同一 Pane 的 `agent_session.source` 最终是哪一个？

按真实代码分三处看：

| 代码位置 | 比较内容 | 作用范围 | `source` 不同时 |
|---|---|---|---|
| `verify_existing`（496-501） | **只比 `value`** | team 角色登记 | 仍可通过 |
| `name_lead`（919-921） | kind + **`value`**，**不含 source** | Lead 自身 | 仍可通过 |
| `run_drive.check_identity`（1304-1308） | 逐字段比 `kind`/`source`/`value` | 仅 Run 的**工作角色** | 条件性拒绝 |

**方案 A 的影响是条件性的，不是全面的。** 具体：

- `check_identity` 在整个 `run_drive` 中只被用于 Run 的工作角色——`:1360-1366` 是 drive 启动前对 `run["roles"]` 的 `identity_unbound` 预检，`:1385` 核对待结算派单的 `rec["role"]`，`:1423-1425` 核对返工目标角色。**Lead 不在这些检查的范围内**，因此 Lead 的 `source` 发生变化不必触发 `run drive` 失败。
- 只有当**某个工作角色**在登记时保存的 `source` 与实时读到的 `source` 不同时，该角色的 `check_identity` 才会拒绝——即 Happier 写入改变了**该 Worker 的** `agent_session.source` 且与登记值不一致。
- 若团队在 Happier 环境下从零建立（Worker 登记时就已是 `"happier"`），保存值与实时值一致，`run drive` 可正常推进。

因此准确结论是：**方案 A 落地后 `name-lead` 可能可用；`run drive` 是否受影响取决于 Run 中工作角色的 `source` 是否发生漂移，而非取决于 Lead。** 跨环境接管既有 team（原生建、Happier 跑）才是最可能触发拒绝的场景，届时需要 source 归一化设计。归一化方案未设计，本轮不得假定其可行。

判别方法（需写入型操作，本轮未执行）：隔离 Pane 上先让 Herdr 集成写入，再手工 `herdr pane report-agent-session --source happier --agent claude <pane> --agent-session-id <同一 value>`，随后两次 `herdr pane get` 观察 `source`。

**仍需 Owner 批准**：
- B 扩张 `context()` 的拒绝条件边界；
- 竞争窗口二选一（接受 / 终态复核）；
- 跨机场景：iPhone 控制端 + Mac 执行端时，若命令仍在执行端 Mac 的 Pane 内运行，父链本机成立、B 可用；若命令运行在 Pane 之外（手机侧直连 daemon 下发），B 必然 0 命中并 fail closed —— **这是预期行为**，若要支持远端发起只能走方案 C；
- `docs/trial-guide.md` 与 `SKILL.md` 的"Happier 不兼容"表述需同步收窄。

---

## 8. 上游最小修改（若只接受上游修复）

### 8.1 Happier 补 `pane.report_agent_session`

文件：`~/.happier/cli/versions/0.2.17/package-dist/applyRunnerMcpSessionContext-DPRBYk-y.mjs`

| 行 | 符号 | 内容 |
|---|---|---|
| 3853 | `bindHerdrAgentIfNeeded` | 解析 terminal，末尾调 `bindManagedHerdrSession` |
| 3880 | 调用处 | 传入 session/client/terminalId/agent/sessionId |
| 3889 | `bindManagedHerdrSession` 定义 | — |
| 3918 | `report()` | 只发 `pane.report_agent`（`source:"happier"`），从不发 `report_agent_session` |
| `api-BJP7Fl6P.mjs:54914` | `findPane(terminalId)` | 用 `pane.list` 的 `terminal_id` 反查，**paneId 由服务端给出** |

写入点已确定（`report()` 内有权威 paneId），**缺的是权威 vendor session id**：该文件内 `grep claudeSessionId` 命中数为 0；`command-1D3NR-aN.mjs:5800` 的 `readClaudeSessionIdFromSession` 未被导出且用 `session.client.getMetadataSnapshot()`，而本文件 3839 行用 `session.getMetadataSnapshot?.()`，两种访问器并存、未确认 bind 层可用哪一种。**这是本节的 Open Question，不写实现细节。**

参数形态（`PaneReportAgentSessionParams`，protocol 22）：必填 `pane_id`/`source`/`agent`；可选 `agent_session_id`、`agent_session_path`、`seq`、`session_start_source`、`resume_argv`。`source` 应沿用 `"happier"`。

风险：同一 Pane 已有 Herdr 集成的写入者，双写来源的 `source` 必然不同，见 §7 的 Open Question。

### 8.2 Herdr 提供服务端对端身份（长期正解）

accept 后取对端 pid（macOS `LOCAL_PEERPID` / Linux `SO_PEERCRED`），映射到自己的 Pane 注册表，作为请求上下文。完成后 §4 的父链推断可整体删除。属核心 + 协议变更，周期不可控。

---

## 9. 已知限制与未验证项

**已知限制**：`state_file` 只以 `workspace|lead_pane` 为键（269-271），不含 session。若两个独立 Herdr server 出现相同的 workspace+pane ID，它们会命中**同一个账本文件**；原生路径与历史账本**都不受保护**，当前无额外防护。加防护必须给账本或其 key 引入 session 维度，与 §4.3 冲突。§4.3 的 fail closed 规则只在派生路径上能提前暴露冲突。

**未验证项**（不得当作已完成）：
- B 未落地代码，未在真实 Happier Lead 上端到端跑通；
- `source` 仲裁未实测（§7）；
- 跨 session Pane ID 是否唯一无证据（本机仅一台 running server）；
- `happier session create --env` 能否让 `HERDR_*` 穿透统一终端白名单未实测，且即便可行也不应采纳（等同用户自报身份）；
- `pane.report_agent` 与 `pane.report_agent_session` 是否互相覆盖状态未验证。
