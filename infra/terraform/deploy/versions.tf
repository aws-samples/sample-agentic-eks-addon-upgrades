terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # No backend block: state is local (terraform.tfstate in this directory, gitignored).
  # This is a sample. For shared or long-lived use, add an S3 backend with
  # `use_lockfile = true`.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = var.tags
  }
}
