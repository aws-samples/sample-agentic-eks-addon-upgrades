# Install

## Prerequisites

- A non-production AWS account where you can create IAM, Lambda, Step Functions and Bedrock AgentCore resources.
- Amazon Bedrock access to Claude Sonnet (`us.anthropic.claude-sonnet-5` inference profile) in the deploy region.
- An EKS cluster with authentication mode `API` or `API_AND_CONFIG_MAP`.
- Network access from where you run Terraform to the cluster's Kubernetes API server (otherwise see [step 4](#4-onboard-the-cluster)).
- Locally: `uv`, Terraform 1.6+, AWS CLI v2, `make`, `zip`.
- A GitHub token stored in Secrets Manager, so research isn't held back by GitHub's anonymous rate limit.

Check that you are in the account you will deploy to, and that it can see your cluster:

```bash
aws sts get-caller-identity
aws eks list-clusters --region us-east-1
```

## 1. Install the local environment

Clone the repository and run every command from its root unless a step says otherwise:

```bash
git clone https://github.com/aws-samples/sample-agentic-eks-addon-upgrades.git
cd sample-agentic-eks-addon-upgrades
make install
```

`make install` creates the local Python environment in `.venv` with `uv`.

## 2. Configure

```bash
cd infra/terraform/deploy
cp terraform.tfvars.example terraform.tfvars
```

Set the required values in `terraform.tfvars`:

```hcl
target_cluster          = "my-eks-cluster"
approval_email_from     = "you@example.com"
approval_email_to       = ["you@example.com"]
github_token_secret_arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:github-token"
```

> [!NOTE]
> **Upgrade stage.** By default (`upgrade_stage_enabled = false`) a run ends on a readiness report
> and nothing on the cluster changes. Set `upgrade_stage_enabled = true` to let approved upgrades
> run. You can change it later; see [Turn the upgrade stage on or off later](#turn-the-upgrade-stage-on-or-off-later).

Every other setting (`region`, `name_prefix`, `target_kubernetes_version`, lane tuning) is in the
file with its default.

## 3. Deploy

From the repository root:

```bash
make init
make plan
make deploy
```

`make plan` shows what will be created; `make deploy` builds, plans again and applies.

SES emails a verification link to each address the first time it is created. Click it. While
your account is in the SES sandbox, every recipient must be verified.

## 4. Onboard the cluster

```bash
cd infra/terraform/cluster-access
cp terraform.tfvars.example my-eks-cluster.tfvars
terraform -chdir=../deploy output -raw scan_role_arn
```

Set in `my-eks-cluster.tfvars`:

```hcl
cluster_name           = "my-eks-cluster"
platform_scan_role_arn = "<scan_role_arn from above>"
```

```bash
terraform init
terraform workspace new my-eks-cluster
terraform apply -var-file=my-eks-cluster.tfvars
```

> [!NOTE]
> **Cluster API server not reachable from Terraform?** Set `apply_supplementary_rbac = false` and
> apply `rbac.tf`'s read-only ClusterRole through the cluster's own GitOps. Until it exists, CRD
> and APIService reads show up in the report as gaps.

## 5. Start a run

```bash
eval "$(terraform output -raw start_a_run)"
```

To target a different Kubernetes version for one run, add `"target_kubernetes_version":"1.36"` to
the `--input` JSON. Follow the run in the Step Functions console, or:

```bash
aws stepfunctions list-executions --state-machine-arn "$(terraform -chdir=../deploy output -raw state_machine_arn)"
```

## 6. Approve from email

Each approval the run needs arrives by email: follow the link in it. The readiness report (and,
with the upgrade stage on, the upgrade report) is emailed to you when the run finishes.

## Turn the upgrade stage on or off later

Change `upgrade_stage_enabled` in `infra/terraform/deploy/terraform.tfvars` and run `make deploy`.
Only the state machine changes.

## Add another cluster

Repeat step 4 with a new `<cluster>.tfvars` and workspace. Runs against it use that cluster's
`start_a_run` output. Upgrades only ever apply to `target_cluster`.

## Build and validate without deploying

```bash
make build
make validate
```

`make build` writes the Lambda zips to `build/`; `make validate` runs `terraform validate`
against them.
