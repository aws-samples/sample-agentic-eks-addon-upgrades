# EKS add-on upgrades -- developer and deployment entry points.
#
#   make help          list targets
#
# Requires: uv, Terraform >= 1.6, zip/unzip, openssl, AWS CLI v2 (for deploy).

SHELL     := /bin/bash
TF_DIR    := infra/terraform/deploy
TF        := terraform -chdir=$(TF_DIR)

.DEFAULT_GOAL := help

.PHONY: help
help: ## List targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------------------
# Develop
# ---------------------------------------------------------------------------
.PHONY: install
install: ## Create the local environment (uv sync)
	uv sync

.PHONY: test
test: ## Run the unit test suite
	uv run pytest

.PHONY: checks
checks: ## Run every check lane, including the doctests and the frozen-digest pins
	uv run python tests/run_checks.py

.PHONY: lint
lint: ## Lint Python (ruff) and check Terraform formatting
	uv run ruff check .
	terraform fmt -check -recursive infra/terraform

.PHONY: fmt
fmt: ## Format Terraform
	terraform fmt -recursive infra/terraform

.PHONY: scan
scan: ## Security scans: bandit (Python) and checkov (Terraform). CI also runs tflint and gitleaks.
	uvx bandit==1.9.4 -c pyproject.toml -r eks_addon_readiness tools scripts entrypoints -ll -q
	uvx checkov==3.3.26 --config-file .checkov.yaml

.PHONY: generate
generate: ## Regenerate the agent prompts and tool schemas from the Python source
	uv run python -m tools.brief.generate
	uv run python -m tools.research.generate

# ---------------------------------------------------------------------------
# Build and deploy
# ---------------------------------------------------------------------------
.PHONY: build
build: ## Build every Lambda zip and the upgrade-lane zip into build/
	./scripts/build-packages.sh

.PHONY: init
init: ## terraform init (local state)
	$(TF) init

.PHONY: validate
validate: build ## terraform validate (needs the built zips)
	$(TF) init -backend=false -input=false >/dev/null
	$(TF) validate

.PHONY: plan
plan: build ## terraform plan with your terraform.tfvars
	@test -f $(TF_DIR)/terraform.tfvars || { echo "Missing $(TF_DIR)/terraform.tfvars -- copy terraform.tfvars.example and fill it in."; exit 1; }
	$(TF) plan -out=tfplan

.PHONY: deploy
deploy: plan ## Build, plan, then apply the saved plan
	$(TF) apply tfplan
	@rm -f $(TF_DIR)/tfplan

.PHONY: destroy
destroy: ## Destroy everything this sample created (see docs/cleanup.md first)
	$(TF) destroy

.PHONY: clean
clean: ## Remove build output and local caches
	rm -rf build .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +
