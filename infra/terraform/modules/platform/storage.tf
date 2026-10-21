# Stage documents and the fact store.
#
# KEY LAYOUT. Five prefixes, one per stage, each key carrying the digests of the
# inputs that determined the document (see eks_addon_readiness/storage/keys.py,
# whose doctests are the contract):
#
#   snapshots/cluster=<c>/date=<d>/<snapshot>.json
#   inventories/cluster=<c>/date=<d>/snap-<s>.rules-<r>.json
#   gaps/cluster=<c>/date=<d>/snap-<s>.store-<st>.rules-<r>.json
#   decisions/cluster=<c>/target=<t>/date=<d>/snap-<s>.store-<st>.rules-<r>.json
#   plans/cluster=<c>/target=<t>/date=<d>/snap-<s>.store-<st>.rules-<r>.json
#
# Partitioning by cluster first is what makes per-cluster IAM scoping possible,
# and it is why onboarding cluster number two needs no change here.

resource "aws_s3_bucket" "documents" {
  #checkov:skip=CKV2_AWS_62:The notification is aws_s3_bucket_notification.decisions in modules/research; Checkov cannot see across modules.
  bucket = local.bucket_name

  # The account-regional namespace: names ending `-<account>-<region>-an` can only ever be created
  # by this account, so nobody can take the name first or re-create it after a destroy.
  # https://docs.aws.amazon.com/AmazonS3/latest/userguide/gpbucketnamespaces.html
  bucket_namespace = "account-regional"
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Versioning on, because a stage document is evidence. A re-run with the same
# inputs overwrites the same key by design (the key is a function of the inputs),
# and versioning is what keeps the previous bytes recoverable if a rule change
# makes two runs disagree.
resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket     = aws_s3_bucket.documents.id
  depends_on = [aws_s3_bucket_versioning.documents]

  rule {
    id     = "expire-noncurrent-document-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Deny any request not using TLS. Cheap, and the kind of thing a published sample
# should model rather than omit.
#
# kics FALSE POSITIVE, SUPPRESSED WITH EVIDENCE. kics flags this resource as
# "S3 Bucket Policy Accepts HTTP Requests" (query 4bc4dd4c-7d8d-405e-a0fb-57fa4c31b4d9).
# The policy below is a correct and complete TLS denial -- Effect=Deny, Action=s3:* on the
# bucket and bucket/*, Condition aws:SecureTransport=false -- which is exactly the shape the
# rule's `deny_http_requests` helper looks for
# (assets/queries/terraform/aws/s3_bucket_policy_accepts_http_requests/query.rego).
#
# The flag is a PARSER artifact, not a policy gap. kics decodes HCL with the Go `hcldec`
# package, which cannot resolve the cross-resource references in this statement's Resource
# array (`aws_s3_bucket.documents.arn` and "${aws_s3_bucket.documents.arn}/*"). The statement
# then fails to materialise, `deny_http_requests` finds no matching statement, and the rule
# fires. A Checkmarx maintainer confirmed this exact cause and called it a false positive in
# https://github.com/Checkmarx/kics/issues/5489 ("failing in the parsing process due to
# missing targeted resources in the resource array situated inside the policy statement").
#
# NO EFFECT-PRESERVING REWRITE CLEARS IT. The parse failure is driven by the unresolved ARN
# references, and the bucket name is unknowable at author time (account-regional namespace,
# derived at apply from account + region), so the policy cannot be rewritten as a literal
# JSON string the way kics' own passing fixtures are. The data-source form in issue #5489
# fails identically. Suppression with this justification is therefore the correct action.
# kics-scan ignore-block
resource "aws_s3_bucket_policy" "documents_tls_only" {
  bucket = aws_s3_bucket.documents.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyNonTlsRequests"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.documents.arn,
        "${aws_s3_bucket.documents.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}

# ---------------------------------------------------------------------------
# Server access logs for the document bucket.
#
# A DEDICATED LOG BUCKET, hardened the same way the document bucket is: public access
# blocked, ownership BucketOwnerEnforced (ACLs off), SSE, versioned, TLS-only, with a 90-day
# expiry so logs do not accumulate forever. `force_destroy = true` so `make destroy` can empty
# and remove it -- access logs are operational data, not the evidence the document bucket holds.
#
# SSE-S3 (AES256), NOT SSE-KMS, AND THAT IS A HARD REQUIREMENT, NOT A CHOICE. S3 server access
# log delivery writes through the S3 log-delivery system, which cannot use an SSE-KMS target
# bucket -- deliveries to a KMS-encrypted bucket silently fail. So this one bucket stays on
# SSE-S3 even though the rest of the sample could take a CMK, and it is NOT given the logs CMK
# for that reason.
# https://docs.aws.amazon.com/AmazonS3/latest/userguide/ServerLogs.html
#
# The derived name keeps the account-regional suffix the namespace requires
# (`-<account>-<region>-an`), with `-logs` before it so it never collides with the document
# bucket's own name.

resource "aws_s3_bucket" "access_logs" {
  # This IS the server-access-log target bucket; a bucket cannot log to itself (infinite
  # regress, which S3 refuses). It is the one bucket in the sample with no access logging.
  #checkov:skip=CKV_AWS_18:This is the access-log target bucket; a bucket cannot log to itself.
  #checkov:skip=CKV2_AWS_62:A write-only access-log sink needs no event notifications; nothing consumes object-created events here.
  bucket = local.access_log_bucket_name

  # Same account-regional namespace guarantee as the document bucket: only this account can
  # ever create this name, and nobody can re-create it after a destroy.
  bucket_namespace = "account-regional"

  # Access logs are operational data, not evidence. Let destroy empty and remove the bucket.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket                  = aws_s3_bucket.access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# BucketOwnerEnforced disables ACLs entirely. Modern S3 log delivery writes with
# bucket-owner-enforced ownership and does not need the legacy log-delivery-group ACL, so this
# is both the hardened choice and the working one.
resource "aws_s3_bucket_ownership_controls" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# SSE-S3, deliberately -- see the resource header. Log delivery cannot target an SSE-KMS bucket.
resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket     = aws_s3_bucket.access_logs.id
  depends_on = [aws_s3_bucket_versioning.access_logs]

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    # Access logs are useful for a short window; 90 days matches the document bucket's
    # noncurrent-version horizon so one retention story covers both.
    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Allow the S3 logging service to deliver this bucket's own access logs, and deny every
# non-TLS request. Two statements:
#   1. logging.s3.amazonaws.com may PutObject, scoped by aws:SourceArn (the document bucket)
#      and aws:SourceAccount (this account) so no other bucket -- and no other account's
#      bucket of the same name -- can write here. This is the modern replacement for the
#      log-delivery-group ACL and is what BucketOwnerEnforced requires.
#   2. Deny any request not using TLS, matching the document bucket.
#
# kics FALSE POSITIVE on the TLS statement, suppressed for the same reason the document
# bucket's policy is -- the Resource array references `aws_s3_bucket.access_logs.arn`, which
# kics' `hcldec` decoder cannot resolve, so `deny_http_requests` cannot see the Deny. See the
# evidence on `aws_s3_bucket_policy.documents_tls_only` above and Checkmarx/kics#5489.
# kics-scan ignore-block
resource "aws_s3_bucket_policy" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowS3ServerAccessLogDelivery"
        Effect    = "Allow"
        Principal = { Service = "logging.s3.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.access_logs.arn}/*"
        Condition = {
          ArnLike      = { "aws:SourceArn" = aws_s3_bucket.documents.arn }
          StringEquals = { "aws:SourceAccount" = local.account_id }
        }
      },
      {
        Sid       = "DenyNonTlsRequests"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.access_logs.arn,
          "${aws_s3_bucket.access_logs.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })
}

# Server access logging for the document bucket, delivered to the dedicated log bucket above.
# This is what clears "S3 Bucket Logging Disabled" (kics) and CKV_AWS_18 on the document bucket.
resource "aws_s3_bucket_logging" "documents" {
  bucket        = aws_s3_bucket.documents.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "documents/"
}

# ---------------------------------------------------------------------------
# The fact store.
#
# Keyed component_id (HASH) + version_series (RANGE), a single-shape schema so an
# operator reads one shape rather than two. `entry` holds the ComponentKnowledge
# JSON; DynamoDbFactStore validates it on read.
#
# This is a dedicated table for this pipeline's facts.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "facts" {
  name         = local.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "component_id"
  range_key    = "version_series"

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

  # A promoted fact changes what a verdict says. Deleting the table by accident
  # would silently turn every verdict UNRESOLVED, which reads as "we checked and
  # do not know" rather than as the outage it is.
  deletion_protection_enabled = true
}
