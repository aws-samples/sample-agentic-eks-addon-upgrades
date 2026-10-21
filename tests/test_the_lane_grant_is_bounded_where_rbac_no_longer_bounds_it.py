"""The lane holds cluster-admin, so the things that DO bound it have to be exact.

WHAT THIS FILE USED TO BE. It asserted that the Terraform type lists granting the write lane agreed
with `FENCED_KINDS` in Python -- two lists in two languages describing one set, free to drift in
opposite silent directions. Those type lists are gone. `AmazonEKSClusterAdminPolicy` is `*`/`*`/`*`,
associated to the lane's access entry at `Lane grant` and revoked when `Lane cleanup` deletes it.

WHY THE LANE STOPPED ENUMERATING PERMISSIONS. Twice, and the second time in three places. A
per-component fence naming each object by `resourceNames` did not generalize to a new add-on. One
generic type-scoped role was closer and still wrong: a `create` on secrets RBAC could not match
because the rule carried `resourceNames`; then `replicasets` list, which `helm rollback --wait`
needs; with `pods` and `events` behind it. Each was one line, and each surfaced only by running a
real chart -- at the cost of a human approval and a full pipeline run per discovery.

SO THE BOUNDS MOVED AND THIS FILE FOLLOWS THEM. Four, of which only the last is checkable
without an AWS call:

  TIME       the association hangs off the access entry and dies with it -- asserted by
             `test_the_upgrade_lane_always_closes_its_access_entry.py`, which walks the graph
  WHO        only the CodeBuild role; the agent holds read-only RBAC -- asserted by
             `test_the_upgrade_agent_holds_no_write_path.py`
  WHAT       the approved chart's render, applied by Helm, which refuses to adopt an object
             another release holds -- Helm's check, not this repository's; the code fence kept a
             list of reviewed KINDS for a while and that went the way of the other per-chart lists
  PREFLIGHT  the buildspec must assert only properties the design actually guarantees, and must
             exercise the code fence with a document that has to be refused

THE PREFLIGHT CHECK HAS CAUGHT THE SAME CLASS OF MISTAKE TWICE, which is why it is a test and not a
review item. First an assertion that the lane could not patch `Deployment/coredns` -- true under the
per-component fence, false under the generic role. Then assertions of no cluster-wide write and no
`deletecollection` -- true under the generic role, false under cluster-admin. Each refused EVERY
lane, correctly, against a design that had moved beneath it. A stale negative assertion is the most
expensive check here: it reads as a security finding and costs an approval to discover.

`*.tf` and the buildspec are parsed as TEXT, not evaluated: this suite runs with no Terraform.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# THE MODULE THAT USED TO HOLD THE LANE'S RBAC. Asserted ABSENT rather than read: it held a write
# Role and ClusterRole, then only the agent's reader ones, and now neither -- the lane's write grant is
# `AmazonEKSClusterAdminPolicy` per run and the agent reads through a Gateway with its own standing
# entry. A path that no longer exists is the strongest form of "no Role misrepresents the boundary".
LANE_MODULE = ROOT / "infra/terraform/modules/upgrade-lanes"
PLATFORM_TF = ROOT / "infra/terraform/modules/platform/upgrade_stage.tf"
PLATFORM_TFVARS = ROOT / "infra/terraform/deploy/terraform.tfvars.example"
BUILDSPEC = ROOT / "infra/terraform/modules/platform/upgrade_buildspec.yml"

CLUSTER_ADMIN_POLICY = "AmazonEKSClusterAdminPolicy"

# Tokens the preflight must NOT assert any more, each with why it became false. Kept as data so
# adding the next one is a line rather than a new test.
RETIRED_PREFLIGHT_ASSERTIONS = (
    ("deployments/coredns", "the lane may patch a neighbour in RBAC terms, on purpose"),
    ("can-i '*' '*'", "cluster-admin IS cluster-wide write, so this refuses every lane"),
    ("can-i deletecollection", "cluster-admin grants deletecollection, so this refuses every lane"),
)


def _uncommented(path: Path) -> str:
    """The file with comment-only lines dropped, so a comment cannot satisfy an assertion.

    Every check below tests for the presence or absence of a token, and this project's Terraform
    carries long comments that quote the very tokens being searched for -- including retired ones.
    Without this, `deployments/coredns` inside a comment explaining why it is no longer asserted
    would keep that assertion green forever.
    """
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_no_rbac_module_survives_to_misrepresent_the_lanes_scope() -> None:
    """A partial Role beside cluster-admin would be the worst of both shapes.

    It can grant nothing cluster-admin does not already grant, so it can never fail closed -- but it
    WOULD read like the boundary, and a reviewer asking "what can this lane touch" would find a
    narrow Role and believe it. Either shape is honest alone; both together is not.

    THIS USED TO ALLOW A `reader` ROLE AND NOW ALLOWS NONE. The check was "every Role in this module
    is the agent's reader", which was right while the agent held a read-only access entry of its own.
    It reads the cluster through an AgentCore Gateway now, under a standing Terraform-managed entry
    carrying `AmazonEKSAdminViewPolicy`, so the lane mints no identity for it and the module is deleted.
    """
    assert not LANE_MODULE.exists(), (
        f"{LANE_MODULE} is back. The lane's write grant is {CLUSTER_ADMIN_POLICY} per run and the "
        "agent reads through a Gateway, so any Role here grants nothing extra and misleads anyone "
        "reading this repository for the lane's access scope."
    )


def test_the_grant_is_cluster_admin_and_revocation_stays_one_call() -> None:
    """Named in the committed definition, not taken from a payload.

    The IAM statement conditions on the principal but NOT on the policy ARN: a condition key for it
    could not be verified against AWS's reference, and an unverified condition key denies silently
    instead of failing loudly. The one reliable narrowing is that this string lives in git, which is
    what makes it worth asserting.
    """
    platform = _uncommented(PLATFORM_TF)
    assert CLUSTER_ADMIN_POLICY in platform, (
        f"{PLATFORM_TF.name} no longer names {CLUSTER_ADMIN_POLICY}. Returning to enumerated RBAC "
        "is a real design change -- update this file deliberately rather than dropping this."
    )
    assert "associateAccessPolicy" in platform, (
        "the `Lane grant` state is gone, so the entry is created and granted nothing. Every lane "
        "would 403 on its first write, after a person had already approved the upgrade."
    )
    assert "DisassociateAccessPolicy" not in platform, (
        "the stage grants `eks:DisassociateAccessPolicy`. An association is keyed by (cluster, "
        "principalArn) -- the entry's own identity -- so `DeleteAccessEntry` is the revocation. A "
        "second call that must succeed on the way out is a second way to leave a standing grant."
    )


def test_the_preflight_asserts_only_what_the_design_guarantees() -> None:
    """The check that has caught the same class of mistake twice. See the module docstring."""
    live = _uncommented(BUILDSPEC)

    for retired, why in RETIRED_PREFLIGHT_ASSERTIONS:
        assert retired not in live, (
            f"the buildspec still asserts {retired!r}, now false by design: {why}. A stale "
            "negative assertion refuses every run and reads as a security finding."
        )

    assert "fence-check" in live, (
        "the buildspec no longer runs `fence-check`. With cluster-admin on the lane it is the only "
        "pre-mutation check that the render ships the approved version."
    )
    assert live.count("fence-check") >= 2 and "off-lane.yaml" in live, (
        "the preflight runs `fence-check` only on the real manifest, which cannot tell a working "
        "fence from one that accepts anything. It must also be handed a render that must be "
        "refused -- one that does not ship the approved version."
    )
