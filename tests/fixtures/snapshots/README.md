# Snapshot fixtures

## `my-eks-cluster-live.json`

A sanitized Stage 1 snapshot of a real EKS cluster at Kubernetes 1.35, captured
2026-09-01. The cluster name has been replaced with `my-eks-cluster`; the snapshot
carries no account IDs or ARNs.

**Why this one.** It has eleven workloads including several Helm-installed CNCF
components (keda, metrics-server, opensearch-proxy) with real
`meta.helm.sh/release-name` annotations. That matters for the resolve loop: a
Helm-installed component with no stored fact is the researchable case. Nothing here
is synthetic -- it is a real reading of a real cluster, which is what a snapshot is.

**How it is used for the fleet-economics demonstration.** The same snapshot, with
only `cluster.name` and `snapshot_id` changed, stands in for two clusters running the
same add-ons. That is precisely the acceptance condition: two clusters, one shared
fact store, and the second must ask no questions the first already answered.
