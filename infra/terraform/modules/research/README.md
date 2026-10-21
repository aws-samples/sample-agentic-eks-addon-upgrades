# `modules/research` — Stage 6 and fact approval

Applied through [`../../deploy`](../../deploy/). It receives the document bucket and the fact
table **by name** from `platform` and never manages either.

| Created here | What it is |
|---|---|
| `<prefix>-research-harness` | The research agent (Bedrock AgentCore harness) and its gateway |
| `<prefix>-tool-fetch-source` | The agent's only way to read a page. Every body it fetches is recorded under `research-evidence/` before anything reads it. Also reused by the brief agent. |
| `<prefix>-research-harness-proxy` | What the state machine's `Research` Map invokes; it calls the harness |
| `<prefix>-judge` | The deterministic judge. No model: checks every quote against the fetched bytes |
| `<prefix>-approval` | The `Approval` state: builds the approval page and mails the links |
| `<prefix>-approve` | **The only function that writes a fact.** IAM-authenticated function URL |
| `<prefix>-approve-link` | Redeems a single-use emailed link and invokes `-approve`. Holds no fact-table write. |
| `<prefix>-approver` | The role a person assumes to approve with a signed request |
| `<prefix>-decision-watcher` | Resumes the run when a decision is written to S3 |
| `<prefix>-approval-tokens` | DynamoDB: single-use approval grants, with TTL |
| SES identities | `approval_email_from` and each `approval_email_to` |

## Who can write a fact

Only `<prefix>-approve` holds `dynamodb:PutItem` on the fact table, and it accepts a request
only from the `<prefix>-approver` role, or from
`-approve-link` after it redeems a valid grant. The research agent, the judge and the sweep
have no fact-table write. The sweep (in `platform`) writes marks to a separate table for the
same reason.

`denied_approver_role_names` lists roles that must never be able to assume the approver role
(for example, the tool roles of an agent that reads approval pages). It is empty by default,
because IAM rejects a trust policy that names a role that does not exist.
