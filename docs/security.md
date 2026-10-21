# Security

This page explains what this sample can access, who has to approve what, how your data is
protected, and what you should review before you deploy it. It is sample code: review it against
your own requirements first.

## In short

* **Readiness checks are read-only.** The sample only reads your clusters unless you turn the
  upgrade stage on, and then it can change only the one cluster you name.
* **No agent can change anything.** Agents read and propose. Every change is made by
  deterministic code, using a version a person approved.
* **Three approvals stand between a proposal and a change**, and the last one requires an IAM
  identity that already administers the cluster.
* **Every function has its own IAM role**, limited to the S3 paths and tables it needs.
* **Data is encrypted at rest and in transit**, and every source an agent read is kept for audit.

## What you are responsible for

AWS secures the services this sample runs on. You decide:

* who receives the approval emails, because anyone who can read those mailboxes can approve facts
  and upgrade steps;
* which clusters you onboard, and which one cluster the upgrade stage may change;
* your account's guardrails, such as SCPs, CloudTrail, GuardDuty and AWS Config;
* whether each fact and each upgrade step is right, before you approve it.

## What can access your cluster

| Who | Access | When |
|---|---|---|
| Scan | `AmazonEKSViewPolicy` on each onboarded cluster. It cannot read Kubernetes Secrets | Every run |
| Upgrade agent | `AmazonEKSAdminViewPolicy` on the upgrade target cluster, through the EKS MCP server's read-only tools. It can read Secrets, but cannot change anything | Only when an upgrade lane needs a diagnosis |
| Upgrade lane | Write access to **one component only**: a temporary access entry, created just before the upgrade and removed after, even on failure | Only for steps you approved and authorised |

* The upgrade lane refuses to apply any object that does not belong to the component it is
  upgrading.
* The upgrade infrastructure is scoped to `target_cluster`, and no run reaches it unless
  `upgrade_stage_enabled` is `true`. The upgrade agent's read access on that cluster exists
  either way.
* To remove the sample's access to a cluster, destroy that cluster's `cluster-access` stack.

## The three approvals

| Approval | Who can give it | How it is checked |
|---|---|---|
| Fact approval | Anyone with the approval mailbox, or the `<prefix>-approver` IAM role | Single-use, signed link |
| Upgrade approval | Anyone with the approval mailbox | Single-use, signed form, per step and per cluster |
| Release authorisation | An IAM principal that already has a cluster-admin access entry on the cluster | AWS Systems Manager approval, signed with that principal's identity |

* **An email link proves less than an IAM identity.** The audit record marks email approvals as
  `EMAIL_LINK_TOKEN`, and that is why an upgrade also needs the release authorisation.
* **The release gate fails closed.** If no cluster administrator can be found, or the access
  entries cannot be read, the run stops at `Release rejected`.
* **Link scanners are handled.** Some mail scanners open links in advance. That uses up the
  single-use link instead of approving twice, and the run continues only when you select
  **Done**.

## How data is protected

| Where | Protection |
|---|---|
| S3 document bucket | Public access blocked, TLS required, encrypted, versioned, access logs on |
| S3 access-log bucket | Public access blocked, TLS required, encrypted with SSE-S3, logs expire after 90 days |
| DynamoDB tables | Encrypted. The fact tables also have point-in-time recovery and deletion protection |
| CloudWatch Logs | Encrypted with a customer-managed KMS key that rotates, kept for 30 days |
| Approval tokens | Never written to S3, stage results or logs |
| GitHub token (optional) | Read from AWS Secrets Manager at runtime |

Every page an agent fetched is stored before anything reads it, so each fact can be traced to the
exact source it came from. The upgrade lane downloads Helm and kubectl at pinned versions and
checks their SHA-256 hashes.

## Review before you deploy

* **SES sandbox.** Approval emails go only to verified addresses. Leaving the sandbox is your
  decision.
* **Approve with care.** A fact is only as good as its source. Read the quote and the source link
  on the approval page before you approve.
* **Encryption keys.** S3, DynamoDB and Lambda use AWS-owned or AWS-managed keys. Switch them to
  customer-managed keys if your policies require it. The access-log bucket must stay on SSE-S3,
  because S3 cannot deliver access logs to an SSE-KMS bucket.
* **KMS key.** The log key costs about $1 a month. On teardown it waits 7 days before it is
  deleted; see [Clean up](cleanup.md).
* **Log retention.** Logs are kept for 30 days (`log_retention_days`). Enable CloudTrail data
  events if you need an audit of S3 and Lambda access.
* **Network.** Lambda functions run outside a VPC and reach GitHub and other sources over the
  internet. Put them in a VPC with egress controls if you need to.

## Static analysis

CI runs bandit, checkov, tflint and gitleaks on every pull request. Checks skipped on purpose,
such as VPC Lambdas, X-Ray and dead-letter queues, are listed with their reasons in
`.checkov.yaml` or inline at the resource.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
