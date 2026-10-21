# Terraform

| Directory | Applied | Purpose |
|---|---|---|
| [`deploy/`](deploy/) | once | The root you apply. Wires the four modules together. |
| [`cluster-access/`](cluster-access/) | once per cluster | A read-only role and EKS access entry for one cluster. |
| [`modules/platform/`](modules/platform/) | via `deploy/` | Document bucket, fact and mark tables, stages 1-5 and 7 (resolve), the sweep, the state machine, the release gate, the upgrade stage |
| [`modules/research/`](modules/research/) | via `deploy/` | Research agent, judge, fact approval |
| [`modules/brief/`](modules/brief/) | via `deploy/` | Upgrade brief agent |
| [`modules/upgrade/`](modules/upgrade/) | via `deploy/` | Triage agent and its read-only EKS MCP gateway |

The modules declare no provider; `deploy/versions.tf` does. State is local (no backend block). Shared resources
(the bucket and the fact table) are created by `platform` and passed to the other modules as
names and ARNs, so only one module ever manages them.

Install steps: [docs/install.md](../../docs/install.md).
Clean up: [docs/cleanup.md](../../docs/cleanup.md).
