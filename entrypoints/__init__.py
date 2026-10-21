"""Lambda entry points: the functions Terraform's `handler = "entrypoints.<module>.<fn>"` names.

They hold no logic -- each is one line into `eks_addon_readiness/stages/`. There are four
modules rather than one because each Lambda zip ships exactly one of them:
`research_harness`, `brief_harness` and `upgrade_harness` call AgentCore `InvokeHarness`, which
needs a newer boto3 than the Lambda runtime provides, so their zips vendor it; `stages` ships
none and uses the runtime's own.
"""
