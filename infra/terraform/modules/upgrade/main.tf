# The upgrade agent: the model that triages a failed upgrade, and the proxy that asks it.
#
# THE THIRD HARNESS IN THIS SYSTEM, AND THE ONLY ONE WITH NO GATEWAY. The research agent transcribes
# what a compatibility page says; the brief agent reads a CHANGELOG and judges what an operator would
# care about; each reaches a Gateway because each must FETCH something, and each fetch tool exists so
# the bytes are recorded before the model sees them. This agent fetches nothing. By the time it is
# asked anything, deterministic code has already captured pod status, the pods' own logs including the
# previous container, the namespace's events, `helm history`, `helm status` and the values and
# manifest actually applied, and SEALED them into a manifest. That is the evidence, so it goes in the
# prompt.
#
# WHAT THE ABSENCE OF A GATEWAY BUYS, and it is more than one fewer resource:
#
#   THE LANE FENCE NEEDS NO ENFORCEMENT ON THIS SIDE. A tool taking a `component` argument is a tool a
#   model can point at a neighbour, and the hard requirement -- while troubleshooting one component,
#   do not touch another -- would then rest on a check inside a tool handler. With no tool, the
#   agent's entire input is one component's own bundle, assembled by code the Map iteration handed the
#   component to. There is no argument to get wrong.
#
#   THERE IS NO CREDENTIAL ON THE MODEL'S SIDE AT ALL. `infra/terraform/modules/brief/harness.tf` buys "the agent
#   cannot see anything it did not fetch through a recording tool" with a policy that omits s3 and
#   dynamodb. Here it is true by construction.
#
# IT DECIDES WHETHER A PERSON IS NEEDED, NOT WHETHER TO ROLL BACK. The human's instruction was that
# the agent rolls back without asking, and the code does that: the deterministic default for an
# unhealthy component is ROLLBACK, and it holds when this agent is absent, unreachable, throttled or
# answers something that will not validate. What a model is good for here is the other question --
# would returning the component hide something somebody must see. `TriageAction` has three members and
# no fourth, so a verdict asking for anything else cannot be constructed.
#
# GATED, AND THE WHOLE STAGE WORKS WITHOUT IT. `platform` takes this component's function ARN as a
# variable defaulting to null; with it null the lane never calls out and rolls back deterministically.
# So this is the one piece of the upgrade stage whose absence degrades rather than breaks -- which is
# the same property `infra/terraform/modules/brief` has, and for the same reason: nothing advisory may gate a run.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  name_prefix = var.name_prefix
  partition   = data.aws_partition.current.partition

  package = "${path.module}/../../../../build/upgrade_harness.zip"

  # THE DEPLOYED INSTRUCTION, READ FROM THE FILE THE TESTS COMPARE AGAINST. The text lives in
  # `upgrade_lane/prompt.py::SYSTEM_PROMPT`, beside the contract it describes and inside the prompt
  # ruleset digest, and `agents/readiness_upgrade/system-prompt.md` is generated from it.
  # `scripts/build-packages.sh` fails the build when the two disagree and
  # `tests/test_the_upgrade_agent_holds_no_write_path.py` asserts the same equality in the suite -- so
  # a prompt nobody reviewed cannot reach the `system_prompt` argument below.
  system_prompt = file("${path.module}/../../../../agents/readiness_upgrade/system-prompt.md")

  bucket = coalesce(
    var.document_bucket_name,
    "${var.name_prefix}-${local.account_id}-${var.region}-an",
  )
  bucket_arn = "arn:${local.partition}:s3:::${local.bucket}"

  effective_harness_arn = coalesce(
    var.upgrade_harness_arn,
    one(aws_bedrockagentcore_harness.upgrade[*].arn),
  )
}
