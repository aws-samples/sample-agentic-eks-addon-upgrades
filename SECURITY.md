# Security

## Disclaimer

This project is sample/educational code and is NOT intended for production use without
additional security hardening. See the [production hardening checklist](#production-hardening-checklist)
below, and the detailed security notes in [docs/security.md](docs/security.md) covering what this
sample can access, the three-approval model, and how data is protected.

## Reporting a vulnerability

If you discover a potential security issue in this project, notify AWS/Amazon Security through the
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/) or by
email to [aws-security@amazon.com](mailto:aws-security@amazon.com). Do **not** create a public
GitHub issue.

## Security model in brief

This is sample code. Review it against your own requirements before you deploy it, and deploy it
to a non-production account first. The full model is in [docs/security.md](docs/security.md).

* **Readiness checks are read-only.** The sample reads your clusters with `AmazonEKSViewPolicy`.
  It changes a cluster only when you set `upgrade_stage_enabled = true`, and then only the one
  `target_cluster` you name.
* **Agents read and propose; they never change anything.** Every change is made by deterministic
  code, using a version a person approved.
* **Three approvals stand between a proposal and a change.** The last one, the release
  authorisation, requires an IAM principal that already has a cluster-admin access entry on the
  cluster, and fails closed if none can be found.
* **Least-privilege IAM.** Every function has its own role, scoped to the S3 prefixes, tables and
  SES sender identity it uses.
* **Encryption.** S3 requires TLS and every store is encrypted at rest. CloudWatch Logs use a
  customer-managed KMS key that rotates.
* **No secrets in code, environment variables or logs.** Approval tokens are never written to S3,
  stage results or logs. The optional GitHub token is read from AWS Secrets Manager at runtime.
* **Pinned dependencies.** Python dependencies are locked in `uv.lock` and pinned in the build
  scripts. Helm and kubectl are downloaded at pinned versions and checked against SHA-256 hashes.

## AWS services used

- **Amazon EKS**: the clusters the sample assesses and, with the upgrade stage on, upgrades.
- **AWS Lambda**: every pipeline stage (scan, research, brief, approvals, upgrade decision, report).
- **AWS Step Functions**: orchestrates the pipeline and owns retries and failure recording.
- **Amazon S3**: stage outputs, fetched sources, approval pages and reports.
- **Amazon DynamoDB**: approved facts, fact marks and single-use approval tokens.
- **AWS KMS**: the customer-managed key that encrypts CloudWatch Logs.
- **Amazon SES**: approval and report emails.
- **Amazon CloudWatch Logs**: logs from every stage and lane.
- **AWS CodeBuild**: runs the upgrade lane.
- **Amazon Bedrock AgentCore**: runs the research and brief agents.

## Known security considerations

These are accepted for a sample and listed in the hardening checklist below.

| Item | Rationale |
|------|-----------|
| CloudWatch log retention defaults to 30 days | Sized for a sample. `log_retention_days` lets deployers raise it |
| DynamoDB tables use AWS-owned encryption, not a customer-managed key | A per-organisation key-ownership choice. The tables are encrypted at rest either way |
| Lambda functions run outside a VPC, with no DLQ, X-Ray tracing or KMS-encrypted environment variables | The functions need internet egress to reach GitHub and upstream docs. Step Functions owns retries and failure recording. No secrets are stored in environment variables |
| CodeBuild project uses an AWS-managed key | Consistent with the DynamoDB key-ownership choice |
| No point-in-time recovery on the upgrade-approval token table | Deliberate: tokens are single-use with a TTL, and restoring a backup would bring back spent approval tokens |

## Production hardening checklist

The sample's defaults suit a demo. Before you run it long-term, consider:

- [ ] **SES.** Approval mail goes only to verified addresses while the account is in the SES
  sandbox. Anyone who can read the approval mailbox can approve facts and upgrade steps, so
  restrict who receives it.
- [ ] **Log retention.** `log_retention_days` defaults to 30. Raise it, for example to 365, to
  keep the history you need for incident investigation, and enable CloudTrail data events if you
  need an audit of S3 and Lambda access.
- [ ] **Customer-managed keys.** The DynamoDB tables, S3, Lambda environment variables and the
  CodeBuild project use AWS-owned or AWS-managed keys. Switch them to a customer-managed key, as
  the CloudWatch Logs key in `kms.tf` does, if your policies require key control. The access-log
  bucket must stay on SSE-S3.
- [ ] **Point-in-time recovery.** It is on for the fact tables. It is off for the upgrade-approval
  token table on purpose: restoring a backup would bring back spent single-use approval tokens.
- [ ] **Tracing.** Enable AWS X-Ray active tracing on the Lambda functions for end-to-end
  debugging.
- [ ] **Network.** Lambda functions run outside a VPC and reach GitHub and other sources over the
  internet. Place them in a VPC with egress controls if you need to.
- [ ] **Failure handling.** Step Functions records retries and failures. Add dead-letter queues if
  you invoke the functions any other way.
- [ ] **Account guardrails.** Run the sample under your own SCPs, CloudTrail, GuardDuty and AWS
  Config.

## Resource cleanup

See [docs/cleanup.md](docs/cleanup.md) for full teardown instructions. In short: offboard each
cluster, empty the versioned document bucket, then run `make destroy`. The CloudWatch Logs KMS
key is scheduled for deletion with a 7-day waiting period.

## Dependencies

Python dependencies are locked in `uv.lock`; the build scripts pin the same versions exactly.

| Dependency | Notes |
|------------|-------|
| boto3 | AWS SDK for Python. Pinned in `scripts/build-packages.sh` to the `uv.lock` version |
| urllib3 | HTTP client. Bumped to 2.8.0 to resolve CVE-2026-97687, CVE-2026-97688 and CVE-2026-97689 |
| pyjwt | JWT handling (transitive). Bumped to 2.15.1 to resolve CVE-2026-101918 and CVE-2026-102275 |
| pyyaml | YAML parsing. Pinned to an exact version in the upgrade lane's buildspec |

Re-run `uv lock --upgrade` periodically and track advisories for these packages.

## Static analysis

CI runs bandit, checkov, tflint and gitleaks on every push to `main` and every pull request. Checks skipped on purpose
(VPC placement, X-Ray, dead-letter queues, reserved concurrency, code signing) are listed with
their reasons in `.checkov.yaml` or inline at the resource.
