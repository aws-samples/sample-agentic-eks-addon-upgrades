# The marks table: created here, read by three stages, written only by the sweep.
#
# A mark is how a stored fact stops answering -- written by the freshness sweep when the
# fact's upstream source has moved, or by a human revoking a fact that is wrong. The stages
# that read the fact store have to see them, or a withdrawn fact keeps driving verdicts.
#
# MOVED INTO THIS MODULE with the sweep itself, by the user's decision: the sweep is a stage
# of this pipeline, this is its data, and `infra/terraform/modules/research` -- which created the table
# originally -- keeps Stage 6, the judge and the approval surface. The move was
# a state move and changed no infrastructure. Stage 6,
# the judge and the approver still read the table; they now address it by DERIVED name, the
# same way this module addressed it before the move and the same way it addresses the cluster
# read roles.
#
# THE DEFECT THIS FILE ORIGINALLY CLOSED, and it is why the stages below get the table at all:
# `FACT_MARK_TABLE` was set on infra/terraform/modules/research's functions and on NONE of the five stage
# functions, so `storage/marks.build_mark_store()` returned `EmptyMarkStore` inside `Gaps`,
# `Decide` and `Plan`. Every mark the sweep wrote was invisible exactly where it was
# supposed to act, and the store digest the gap ledger cited did not move. Nothing failed; the
# mechanism reported success while having no effect.

resource "aws_dynamodb_table" "fact_marks" {
  name         = local.fact_mark_table
  billing_mode = "PAY_PER_REQUEST"

  # Same key shape as the fact table on purpose: the mark for a fact sits at the same
  # coordinates as the fact. The range key carries "<series>#<kind>" so a staleness mark and a
  # revocation on one component are two items rather than one overwriting the other -- a
  # revocation must not be erased by an expiry.
  hash_key  = "component_id"
  range_key = "version_series"

  attribute {
    name = "component_id"
    type = "S"
  }

  attribute {
    name = "version_series"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  # Marks are the audit trail for why a fact stopped answering. Losing them loses the answer
  # to "what did last week's assessment rest on", so the table is protected from deletion like
  # the fact table it shadows.
  deletion_protection_enabled = true
}

variable "fact_mark_table_name" {
  description = <<-EOT
    Explicit name for the fact-marks table. Leave null to derive
    "<name_prefix>-fact-marks", which is the name the table already has -- it was created
    under that name by infra/terraform/modules/research before it moved into this module.

    Changing this on a deployed stack DESTROYS AND RECREATES the table, losing every mark and
    therefore the audit trail for every fact that has stopped answering. It exists for a fresh
    deployment that needs a different name, not for renaming a live one.
  EOT
  type        = string
  default     = null
}

locals {
  fact_mark_table = coalesce(var.fact_mark_table_name, "${local.name_prefix}-fact-marks")
}
