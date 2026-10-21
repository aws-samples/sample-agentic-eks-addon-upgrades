# Architecture

This page explains what happens during a run, what you are asked to do, and what touches your
cluster. Reference tables for each stage are at the end.

![Architecture: Step Functions and Lambda stages, three AgentCore agents, SES approval, SSM release gate, CodeBuild upgrade lane, S3 shared store](images/architecture.png)

## In short

* **AWS Step Functions** runs the whole workflow. Each step is an **AWS Lambda** function.
* **Three AI agents** on **Amazon Bedrock AgentCore** do the reading: compatibility research,
  release-note briefs and upgrade troubleshooting. None of them can change your cluster.
* **Deterministic code** makes every decision: what is compatible, in what order to upgrade, and
  whether an upgrade succeeded.
* **You approve** at each point that matters, by email links (Amazon SES) and, before any upgrade,
  an AWS Systems Manager approval signed with your IAM identity.
* **Amazon S3** keeps every document the run produces, so any run can be audited later.

## What happens in a run

A run has five phases. Phases 1 to 4 always run; phase 5 runs only if you turn the upgrade stage on.

### 1. Discover what is installed

`Scan` reads the cluster through a read-only role, and `Inventory` turns that into a list of
components: EKS managed add-ons, Helm releases and other workloads, each with its installed version.

### 2. Check what is already known

The sample keeps a **fact store**: approved statements such as "version 2.4 of this add-on supports
Kubernetes 1.31 to 1.34", each with the source it came from.

* `Sweep` checks whether existing facts are still current upstream.
* `Gaps` lists every component that has no fact for your target version.
* `Resolve` decides which of those gaps are worth researching.

The fact store starts **empty**, so the first run researches every component it finds. Later runs
reuse approved facts and research only what is new.

### 3. Research what is missing, and approve it

* The **research agent** reads each component's own documentation and proposes a fact.
* A **deterministic judge** checks every quote in the proposal against the page the agent
  actually fetched. A proposal it cannot verify is rejected.
* You get an **email with a link to an approval page**. Nothing enters the fact store until you
  approve it.
* Once facts change, the workflow loops back to phase 2 and re-checks with the new knowledge.

### 4. Decide and plan

* `Decide` works out, for each component, whether it is compatible with the target version.
  A component that publishes no compatibility information gets `UNRESOLVED` with a reason,
  never a guess, so you know exactly what to check by hand.
* `Plan` orders the moves: which add-ons to upgrade before the control plane, and which after.
* The **brief agent** reads the release notes between your version and the target, and
  summarises breaking changes, deprecations and known issues for each step. The brief is
  advisory: if it fails, the run still continues.

If the upgrade stage is **off**, the run ends here and mails you a **readiness report**.

### 5. Approve and upgrade (optional)

1. **Approve the plan.** An approval page shows each step with its brief. You approve or skip
   each step, per cluster.
2. **Authorise the release.** A Systems Manager approval asks a cluster administrator to sign
   with their IAM identity. The run records who signed, and when.
3. **Upgrade, one component at a time.** AWS CodeBuild runs one **upgrade lane** per approved
   component, in plan order. The lane runs `helm upgrade` or the EKS `UpdateAddon` API, then checks
   the component's health.
4. **Roll back if needed.** If the health check fails, the lane saves a diagnosis bundle, the
   **upgrade agent** reads the cluster to explain what went wrong, and the lane rolls back.
5. **Get the report.** You receive an upgrade report by email, with the evidence for every step.

## What touches your cluster

| Who | Access | When |
|---|---|---|
| `Scan` | Read-only role, created per cluster by `cluster-access` | Every run |
| Upgrade agent | Read-only, through the EKS MCP server's read-only tools | Only when an upgrade lane needs a diagnosis |
| Upgrade lane | Write access to **one component only**, granted just before the upgrade and removed after | Only for steps you approved and authorised |

No agent holds a write permission. Every change to the cluster is made by code, using a version a
person approved. The lane also refuses to apply anything that is not part of the component it is
upgrading. [Security](security.md) covers each permission in detail.

## The three agents

| Agent | What it does | Can it change anything? |
|---|---|---|
| Research | Reads a component's own docs and proposes which Kubernetes versions it supports | No. A judge verifies its quotes, and a person approves every fact |
| Brief | Summarises release notes between your version and the target | No. Advisory only |
| Upgrade | Explains why an upgrade lane failed, by reading the cluster | No. Read-only tools only |

Each agent has its own system prompt in `agents/<name>/system-prompt.md`.

## Where state lives

| Store | What it holds |
|---|---|
| S3 bucket `<prefix>-<account>-<region>-an` | Every stage's output, approval pages, reports and the pages agents fetched. Versioned and encrypted |
| DynamoDB `<prefix>-facts` | Approved compatibility facts. Only the approval function can write here |
| DynamoDB `<prefix>-fact-marks` | Marks a fact as out of date or revoked |
| DynamoDB approval and upgrade token tables | Single-use approval links, which expire |

Each step reads its input from S3 and writes its output back to S3, instead of calling the next
step directly. That is what makes a run replayable and auditable.

## Reference

### Stages

Each stage does one job, so that its output can be stored, checked and reused on its own.

| Phase | Stage | What it does | Model? |
|---|---|---|---|
| 1 | Scan | Takes a read-only snapshot of what is running on the cluster | no |
| 1 | Inventory | Turns the snapshot into a list of components and their installed versions | no |
| 2 | Sweep | Checks that the facts you approved earlier still match their upstream source | no |
| 2 | Gaps | Lists the components that have no approved fact for the target version | no |
| 2 | Resolve | Picks which gaps to research, so an agent is only asked what is actually missing | no |
| 3 | Research | Has the research agent propose a compatibility fact for one component, with quotes | **yes** |
| 3 | Approval | Emails you the proposed facts to approve or reject | no |
| 4 | Decide | Marks each component compatible or not with the target version, from approved facts | no |
| 4 | Plan | Orders the upgrades: what moves before the control plane, and what after | no |
| 4 | Brief | Has the brief agent summarise release notes and breaking changes for each step | **yes** |
| 5 | Upgrade approval | Emails you the plan to approve or skip each step, per cluster | no |
| 5 | Release gate | Asks a cluster administrator to authorise the release with their IAM identity, and records who did | no |
| 5 | Upgrade lane | Upgrades one approved component, checks its health, and rolls back if it fails | only on failure |
| 5 | Upgrade report | Emails you the result of every step, with its evidence | no |

`Decide` and `Plan` also run before research, on what is already known. When you approve new
facts, the run goes back to `Gaps` and repeats phases 2 to 4 with them.

The full list of states and transitions is in the module docstring of `entrypoints/stages.py`,
and `infra/terraform/modules/platform/workflow.tf` defines the state machine.

### Terraform layout

```
infra/terraform/
├── deploy/            the root you apply; wires the modules together
├── modules/
│   ├── platform/      bucket, fact store, workflow stages, state machine, release gate, upgrade stage
│   ├── research/      research agent, approval endpoints, fact approval
│   ├── brief/         brief agent
│   └── upgrade/       upgrade agent and its read-only EKS MCP gateway
└── cluster-access/    applied once per cluster: read-only role and access entry
```

`cluster-access` is a separate root so that onboarding a cluster never touches the shared
deployment: the scan role can already assume any `<prefix>-read-*` role.
