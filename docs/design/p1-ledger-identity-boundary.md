# P1：跨 Session 账本身份边界的检查分层

- 状态：**设计已收敛，已实施**
- 日期：2026-11-05
- 关联：`docs/issues/p1-cross-session-team-ledger-key.md`（问题描述）
- 前置结论：账本主键不变、旧账本不迁移、不重构状态系统

---

## 1. 为什么不能只比 session 名称

账本里记录 `session: "default"` **只是弱证据**，因为：

- Herdr session 可以被 `session stop` + `session delete` 后以同名重建
- 同名重建后，新 session 的 workspace/pane ID 会重新从 `w1:p1` 分配
- 此时 `session` 名称相同、`context` 也相同，但**底层 Agent 完全是另一批进程**

因此 `session` 字段的作用是**快速排除明显的外来账本**，不是身份证明。真正的身份锚点仍是 **Worker 的 `agent_session` UUID**（`kind`/`source`/`value` 三元组）。

**设计原则**：`session` 名称用于「不匹配即拒绝」，`agent_session` UUID 用于「匹配即可信」。两者是 AND 关系，不是替代关系。

## 2. 现有防护的实际边界（实测 2026-11-05）

| 命令 | 现有防护 | 实测 |
|---|---|---|
| `init` 复用既有账本 | `verify_existing` 逐角色比 UUID | 篡改 UUID 后拒绝 ✅ |
| `run start` | 逐角色 `verify_existing` | 拒绝 ✅ |
| `run dispatch` | `verify_existing` + `lookup_agent` | 拒绝 ✅ |
| `run collect` | `verify_existing` + `lookup_agent` | 拒绝 ✅ |
| `run cancel` | 逐角色 `verify_existing` + 状态白名单 | 拒绝 ✅ |
| `run drive` | `identity_unbound` 预检 | 拒绝 ✅ |
| **`run gate`** | 无身份校验 | **可通过** ❌ |
| **`run finish`** | 无身份校验 | **可通过** ❌ |
| **`run pause` / `resume`** | 无身份校验 | **可通过** ❌ |
| **`status`** | 无身份校验（按名字查） | 可能报错状态 ❌ |
| **`run list` / `recover`** | 只比 `context` | 会列出他人 Run ❌ |

现有 11 个 Team、16 个 Run 的 role `agent_session` 覆盖率 **100%（2/2）**，因此 Team 侧的复用路径实际是安全的。缺口集中在 Run 侧的少数命令。

## 3. 检查分层

### 3.1 三种身份判定结果

```python
def ledger_ownership(data, session, path):
    """返回 'verified' | 'legacy'；确定属于其他 session 时 raise。"""
    recorded = data.get("session")
    if recorded is not None:
        if recorded != session:
            raise Error(
                f"账本 {path} 属于 session {recorded!r}，当前 session 为 {session!r}；"
                f"拒绝跨 session 操作。如为多 session 场景，请为每个 session 设置独立的 HTM_STATE_DIR")
        return "verified"
    return "legacy"      # 旧账本，无 session 字段
```

`session` 不匹配 → **无条件拒绝**（含读操作）。这是最强的信号：账本明确记录了它是别人的。

### 3.2 操作分类与最小检查

按「是否产生新派单 / 是否修改账本」分三档：

| 档位 | 操作 | `verified`（session 匹配） | `legacy`（无 session 字段） |
|---|---|---|---|
| **A. 只读** | `status`、`run list`、`run recover`、`run status`、`run stage` | 放行 | **放行**，输出标注 `ledger_session` |
| **B. 安全停止** | `run pause`（仅暂停方向） | 放行 | **放行**，事件记录 `"identity": "unverified"`，**不得派生其他操作** |
| **C. 终态 / 副作用 / 恢复执行** | `init` 复用、`adopt`、`run start`、`run dispatch`、`run collect`、`run gate`、`run cancel`、`run finish`、`run resume`、`run drive` | **仍要求角色身份自证**（见 3.3） | **要求角色身份自证**（见 3.3） |

**关键修正（2026-11-05，Reviewer 审查后）**：C 档的 Worker UUID 校验**不再区分 `verified` / `legacy`**。

原因是 §1 的威胁模型：session 名称可以被重建后复用，`verified` 只说明「账本记录的 session 名等于当前的」，**不说明这些 Worker 还是原来那批**。若 C 档对 `verified` 放行，§1 点名的「同名 session 重建」威胁对修复后的账本依然成立。

因此：
- `session` 字段的作用是**快速排除外来账本**（不匹配即拒绝，全档位）
- `agent_session` UUID 的作用是**证明 Worker 归属**（C 档必查，两个分支都查）

两者是 AND 关系，不是替代关系——这一点现在在实现里落实了。

**`run finish` 归 C 档（Owner 裁决）**：finish 改变 Run 的最终状态，不允许错误 session 完成其他团队的 Run。

**`run resume` 归 C 档**：resume 会让 Run 重新进入可派单状态，等同于恢复执行能力。

**`run pause` 保留 B 档**：它是故障时的安全停止通道。放行时写审计事件，且**不得由 pause 派生任何其他操作**。注意 B 档仍需 session 匹配——外来账本的 pause 同样拒绝（Reviewer 实测确认）。

### 3.3 补充：冲突与不可观察必须分开

`verify_existing` 的失败有多种原因，只有其中一部分是「身份矛盾」：

| 失败原因 | 归类 | 处理 |
|---|---|---|
| `identity changed` | **冲突** | 整体拒绝 |
| `unexpected pane` / `unexpected kind` | **冲突** | 整体拒绝 |
| agent 不存在 / pane 查询不可用 / CWD 漂移 | **不可观察** | 容忍（Worker 会死），只要有其他角色能锚定 |

第一版实现把**所有** `verify_existing` 异常都当作冲突，会把「Worker 活着但读不到」误报为「账本属于别人」，制造新的硬锁死面。现已按原因区分。

### 3.4 目标 Worker 必须单独验证

账本级「至少一个 UUID 匹配」不覆盖具体操作对象。`run dispatch --role X` 和 `run collect --dispatch-id Y` 必须额外验证**目标角色本身**，否则可能出现「A 角色匹配，于是对没有任何身份记录的 B 角色派单」的情况。

实现：`require_target_role()`，在 `run_dispatch` 加载账本时与 `run_collect` 解析出 dispatch 后调用。

### 3.3 档位 C 对旧账本的要求

```python
def require_roles_owned(data, session, path, *, target_role=None):
    """至少一个角色 UUID 匹配可作为归属锚点；任何已发现的冲突则整体拒绝。"""
    verified, total, conflicts = 0, 0, []
    for role in data.get("roles", []):
        if not (role.get("agent_session") or {}).get("value"):
            continue
        total += 1
        try:
            if verify_existing(role, session=session):
                verified += 1
        except Error as e:
            # 区分「不存活/不可观察」与「身份冲突」：
            # 前者是常见故障态，可容忍；后者是明确的归属矛盾，必须拒绝。
            if "identity changed" in str(e) or "unexpected pane" in str(e) or "unexpected kind" in str(e):
                conflicts.append(str(e))
    if conflicts:
        raise Error(f"账本 {path} 存在角色身份冲突，拒绝执行：" + "；".join(conflicts))
    if total == 0:
        raise Error(f"账本 {path} 未记录任何 Worker 身份，无法证明归属；拒绝执行会产生副作用的操作")
    if verified == 0:
        raise Error(
            f"账本 {path} 的全部 Worker 均无法在当前 session 内验证身份；"
            f"无法证明该账本属于当前 session。拒绝执行。"
            f"如确认该账本应归属当前 session，请检查 Worker 是否仍在运行")
    if target_role is not None:
        # 涉及具体 Worker 的操作（dispatch/collect）额外验证目标角色本身。
        role = next((r for r in data.get("roles", []) if r.get("role") == target_role), None)
        if role is not None and not verify_existing(role, session=session):
            raise Error(f"目标 Worker 角色 {target_role} 无法在当前 session 内验证；拒绝针对它的操作")
```

**三条要点（Owner 裁决）**：

1. **至少一个 UUID 匹配**作为最低身份锚点——部分 Worker 死亡是常见故障态，要求全部会让收尾操作在故障时永久锁死。
2. **任何已发现的角色身份冲突 → 整体拒绝**，即使其他角色验证通过。冲突（UUID 变了、pane 变了、kind 变了）是明确的归属矛盾，不是可用性故障，不能容忍。
3. **涉及具体 Worker 的操作额外验证目标角色**（`target_role` 参数）。`dispatch`/`collect` 必须确认目标 Worker 本身可信，不能因为「另一个角色匹配」就放行。

## 4. `pause` 保留 B 档的原因（`finish` 已按 Owner 裁决移入 C 档）

Owner 裁决：**`run finish` 归 C 档**，因为它改变 Run 的最终状态，不能允许错误 session 完成其他团队的 Run。原设计把 finish 归入 B 档的取舍已被否决，此处保留记录以便追溯。

因此 B 档只剩 `run pause`（仅暂停方向）。它的合理性在于：

| 操作 | 是否产生新派单 | 是否修改已有证据 | 前置条件 |
|---|---|---|---|
| `pause` | 否 | 否 | `status == "active"` |

**`pause` 是故障时的安全停止手段**：若要求 Worker 在线或要求身份完全自证，恰好在最需要停止时不可用——Worker 全死时正是最该能停下来的时候。

**约束**：
- 放行时在 events 里记录 `{"identity": "unverified"}`，事后审计可识别
- **不得由 pause 派生任何其他操作**：不自动 resume、不自动 cancel、不触发派单
- `run resume` 单独归 C 档——resume 会让 Run 重新进入可派单状态，等同于恢复执行能力，必须身份验证

**剩余风险（如实记录）**：Session B 仍可对 Session A 的旧账本执行 `pause`，使 A 的 Run 暂停。这是可用性干扰（A 可 resume 回来），不产生错误证据、不误发任务、不改变终态。

## 5. 不引入新的锁死

| 潜在锁死点 | 设计规避 |
|---|---|
| Worker 全死导致无法安全停止 | `pause` 不要求角色在线（档位 B）——这是唯一保留的无身份通道 |
| ~~Worker 全死导致无法收尾~~ | **未规避（有意）**：`finish` 已按 Owner 裁决归 C 档，Worker 全死时拒绝。这是安全换可用性的明确取舍 |
| 部分 Worker 死亡导致无法取消 | 档位 C 只要求「至少一个」可验证，且把「不可观察」与「冲突」分开 |
| session 名重建后旧账本永久不可用 | 有 `session` 字段时按名匹配；无该字段时按 UUID 自证，不受名称影响 |
| 账本无任何 `agent_session` | 明确报错并说明原因，不是静默锁死 |
| Worker 活着但读不到（可观察性故障） | 归类为「不可观察」而非「冲突」，不触发整体拒绝 |

## 6. 对 Issue #2 的影响

`wX` 的 Team 在 `default` session，修复后：

- 新写入的账本带 `session: "default"`，与当前 session 一致 → `verified`
- 旧账本无该字段 → `legacy`，其 Worker 当前 live → 档位 C 可通过

**零影响**。现有 3 个未结束 Run（`0815589f5d80`、`1d0debfb70e0`、`ca9e5107ac10`）的角色身份覆盖率均为 2/2，操作不受阻。

**唯一行为变化**：若这些 Run 的 Worker 全部死亡，档位 C 操作会被拒绝，并给出明确原因。这是有意的 fail closed。

## 7. 测试边界

| 测试 | 覆盖点 |
|---|---|
| `test_new_ledger_session_match` | 新账本 + session 匹配 → 放行 |
| `test_new_ledger_session_mismatch_*` | 新账本 + session 不匹配 → 所有操作（含只读）拒绝 |
| `test_legacy_ledger_readonly_allowed` | 旧账本 → `status`/`run list`/`recover` 放行并标注 unverified |
| `test_legacy_ledger_rejects_after_session_reuse` | **核心**：旧 Session 建账本 → 退出 → 新 Session 复用相同 pane ID、**单 session 环境** → 档位 C 全部拒绝（证伪「按运行数量判断」） |
| `test_same_name_session_recreated_different_uuid` | 同名 session 重建，UUID 不同 → 拒绝（证明不依赖 session 名） |
| `test_legacy_finish_requires_identity` | **Owner 裁决**：旧账本 + Worker 全退出 → `finish` **拒绝**（不再允许无身份收尾） |
| `test_legacy_pause_when_workers_dead` | Worker 全死 → `pause` 仍可用（B 档安全停止），且写 `identity: unverified` |
| `test_resume_requires_identity` | `resume` 归 C 档，旧账本无身份 → 拒绝；同名重建 → 拒绝 |
| `test_role_conflict_rejects_even_if_others_ok` | 一个角色 UUID 冲突 + 另一个正常 → 整体拒绝（冲突优先于「至少一个」） |
| `test_target_role_verified_for_dispatch` | `dispatch`/`collect` 目标角色本身不可验证 → 拒绝，即使其他角色匹配 |
| `test_partial_roles_verified_allows_ops` | 一半 Worker 因**不可观察**而未验证（非冲突） → 档位 C 放行 |
| `test_all_roles_dead_blocks_side_effects` | 全部 Worker 死亡 → 档位 C 拒绝 |
| `test_rejection_has_no_side_effects` | 拒绝时无账本写入、无 Herdr 派单调用 |
| `test_session_list_failure_fails_closed` | `session list` 失败 → 不降级 |
| `test_pause_does_not_derive_actions` | pause 后无自动 resume/cancel/派单 |

## 8. 实施范围

| 文件 | 改动 |
|---|---|
| `scripts/htm.py` | 新增 `ledger_ownership`、`require_roles_owned`；`_require_run_with_session` 增加操作档位参数；`init`/`adopt`/`run_start` 写入 `session` 字段；`status`/`run_list` 标注 unverified |
| `tests/test_regressions_v42.py` | 新增上述测试 |

**不改**：账本主键算法、`context` 三键契约、Run/Team 的既有字段语义、任何既有账本文件。

---

## 9. 实施记录（2026-11-05）

### 9.1 落地内容

`scripts/htm.py`：

| 符号 | 作用 |
|---|---|
| `ledger_ownership(data, session, path)` | 返回 `verified` / `legacy`；session 不匹配时对所有档位 `raise` |
| `require_roles_owned(data, session, path)` | 旧账本证明归属：任一角色 `verify_existing` 抛错即整体拒绝，否则至少一个 UUID 匹配才放行 |
| `_require_run_with_session(runid, tier="C")` | 统一入口，按档位决定是否对旧账本做角色自证；返回 `(data, session, ownership)` |
| `require_run(runid, tier="A")` | 默认只读档位，避免信息类路径意外获得写权限 |
| `_current_team_with_session(tier="C")` | Team 侧同构校验 |

写入点：`init` / `adopt` 的 team 增加 `"session": herdr_session`；`run_start` 的 run 增加同字段。
输出：`status` 与 `run list` 增加 `"ledger_session"` 标注；`run pause` 在旧账本上写 `{"identity": "unverified"}` 事件。

档位落点：`run_gate` / `run_dispatch` / `run_collect` / `run_cancel` / `run_finish` / `run resume` / `run_drive` = C；`run pause` = B；`run status` / `stage` / `recover` = A。

### 9.2 测试

新增 `LedgerIdentityTests` 与 `LedgerProbeTests`（`tests/test_regressions_v42.py`），19 条，覆盖：

- 新账本记录 session、session 不匹配在 A/B/C 三档均拒绝
- 旧账本只读放行 / UUID 匹配时写操作放行
- **旧 Session 退出、新 Session 复用相同 ID（单 session 环境）** → C 档拒绝、A 档放行
- **同名 Session 重建、UUID 不同** → 拒绝（证明不依赖 session 名）
- 一个角色冲突 + 另一个正常 → 整体拒绝（冲突优先于「至少一个」）
- `pause` 无身份可用但记录 `unverified`、`resume` 要求身份、`finish` 要求身份
- 旧账本无任何 Worker 身份 → 拒绝
- 拒绝时账本字节不变
- `pause` 不派生 prompt/start
- **dispatch 校验目标 Worker 本身**，而非仅账本级锚点

全量：`260 passed, 1 skipped, 130 subtests passed`。

**第一轮 Reviewer 审查后的补强（2026-11-05）**：首版实现有两个实质缺陷，均已修复：

1. `verified` 账本在 C 档跳过 UUID 校验 → 改为**两个分支都校验**（§3.2 已同步修正文档）。新增 `test_verified_ledger_also_checks_worker_uuids` 覆盖该路径。
2. `require_roles_owned` 把一切 `verify_existing` 异常当冲突 → 改为按原因区分「冲突」与「不可观察」（§3.3）。新增 `test_unobservable_worker_is_not_reported_as_conflict`。

另补 `require_target_role()`（§3.4），并在 `run_drive` 的错误分类中把身份失败与通用错误分开。

### 9.3 真实环境回归

| 场景 | 结果 |
|---|---|
| 生产默认目录（11 个历史 team + 16 个历史 Run，均无 session 字段） | `status` 正常返回并标注 `legacy`；`run list` 同样标注 |
| 隔离目录 `herdr-team-i6-native`（修复前建的 team/run） | `status` 标注 `legacy`，可读 |
| 旧 team 上的 C 档 `run start` | 成功创建 Run（未锁死） |
| **旧 team 上的完整五阶段 Run** | Run `3e02fd9767a6` `finished`，五阶段全 pass |
| 修复后新建的 Run | 带 `session: "default"`，`ledger_session` 为 `verified` |
| 原生 Herdr 五阶段（修复前代码） | Run `886529c25e33` `finished`，五阶段全 pass |

**关于 `legacy` 与 `verified` 混用**：同一个状态目录里，修复前创建的 Run 为 `legacy`、修复后创建的为 `verified`，这是预期——该标注描述的是「这条账本记录自身是否带 session 身份」，不是「当前进程是否可信」。验收时已确认两者同时存在且互不影响。

### 9.4 已知取舍：`run list` 遇到外来 Run 时整体报错

**现象**：若状态目录里存在一条明确属于其他 session 的 Run（`session` 字段不匹配），`run list` 会整体 `raise`，而不是跳过它继续列出本 session 的 Run。

**实测**：伪造一条 `session: "i6-accept"` 的 Run 放进临时目录，在 `default` 下 `run list` 报
`ERROR: 账本 ... 属于 session 'i6-accept'，当前 session 为 'default'；拒绝跨 session 操作`。

**为什么这样设计**：`ledger_ownership` 对读操作也拒绝，是为了让「跨 session 混用目录」这个错误**立即、显式地暴露**，而不是被静默跳过。若跳过，用户会看到一个「少了几条 Run」的正常输出，从而误以为自己已经隔离好了。

**这个取舍的代价**：在多 session 共用同一状态目录的故障场景下，用户会暂时看不到自己正常的 Run，必须切换到正确的 `HTM_STATE_DIR` 才能恢复。考虑到正确做法本来就是「每个 session 一个状态目录」，这个代价可以接受。

**待 Reviewer 判断**：是否应改为「跳过并标注」而不是整体拒绝。两者都是 fail-closed（都不会让外来账本被当成自己的），区别只在可用性。
