# Seven templates

Six original presets and one guided-operator preset, all using the same supervised Lead behavior.

| Template | Workers | Workflow |
|---|---|---|
| dev-lite | Claude Developer + Claude Reviewer | brief plan, implement, self-test, code review, accept |
| dev-review | Claude Developer + Claude Reviewer | plan, plan review, implement, self-test, code review, accept |
| dev-test | Claude Developer + Claude Tester | brief plan, implement, self-test, independent test, accept |
| full | Claude Developer + Claude Reviewer + Claude Tester | plan, plan review, implement, self-test, independent test, code review, accept |
| research | Claude Researcher + Claude Critic | investigation, challenge, lead verification, synthesis |
| audit | Claude Security + Claude Evidence | security audit, traceability audit, synthesis |
| hermes-guide | Hermes Operator (`-p work`) + Claude Analyst | diagnose, analyze, guided verify |

Templates can be added in `~/.config/herdr-team/team.yaml` under `templates`. A template may `extends` another; roles are merged by role name, workflow list is replaced, settings merge by group. Stages must refer to `lead` or a role in the resolved template. All templates use the same default orchestration and governance unless explicitly overridden.

To change Hermes profile, set or add a distinct `agents` registry entry with `kind: hermes`, `profile: NAME`, `launch_args: ["chat"]`; reference it in the template's `roles.operator`. Do not put `-p` into `launch_args`; the runner inserts it exactly once. To change the Lead, install this Skill in the relevant compatible Agent and start that Agent as Lead in Herdr; no template field changes the running process.
