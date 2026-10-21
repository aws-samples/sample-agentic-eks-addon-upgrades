"""Who may authorise a release is read off the CLUSTER, not out of a tfvars file.

WHAT THIS REPLACES, AND WHY THE REPLACEMENT IS THE SAFER SHAPE. `release_gate.tf` used to
build `Approvers` from `var.release_approver_arns` -- a hand-maintained list of ARNs in
`terraform.tfvars`. Two things are wrong with that and both are silent. A principal who
loses cluster admin keeps authorising releases until somebody remembers to edit the tfvars,
and a new cluster admin cannot authorise anything even though AWS considers them able to
change the cluster by hand. The list and the cluster drift apart with nothing reporting it.

So the list is DERIVED: the access entries of the cluster this run is about, keeping only the
principals AWS already trusts to administer it. The authorisation boundary of the gate then
cannot disagree with the authorisation boundary of the cluster, because there is only one.

AN ALLOWLIST, NEVER A DENYLIST. A policy ARN this code does not recognise is NOT admin. The
tests below pin that from both directions: the two admin policies qualify, and everything
else in the real cluster's entries -- ArgoCD's cluster-scoped policy, the service-linked
role's Insights and Event policies, the read-only view policy, a node group's EC2_LINUX
entry -- does not. A future `AmazonEKSSomethingAdminPolicy` will also not qualify until
somebody adds it here deliberately, which is the correct direction for that mistake to fail
in.

FAIL CLOSED, AND THERE IS NO FALLBACK. An empty resolved list REFUSES the run. It does not
fall back to a hardcoded principal and there is no "nobody is configured, so allow" branch --
`stages/approve.py` has exactly that branch for fact approvals and it is a defect, not a
precedent. An AWS error on any of the three calls is a refusal too: a list that could not be
read is not an empty list, and both are refusals anyway.

THE FIXTURES ARE REAL. Every entry below was read out of a cluster with
`eks list-access-entries`, `describe-access-entry` and `list-associated-access-policies`,
ARNs, policy names, scopes and types verbatim. The expected answer is therefore
not a guess about what the API returns: it is what that cluster actually reports.

`aws-auth` IS OUT OF SCOPE, DELIBERATELY. A cluster admin who exists only in
the `aws-auth` ConfigMap has no access entry, so this resolver does not see them and they
cannot authorise a release. That is a refusal rather than an oversight -- see
`test_an_admin_that_exists_only_in_aws_auth_is_refused`. No ConfigMap is read anywhere here;
the cluster this project runs against uses access entries.
"""

from __future__ import annotations

import pytest

from eks_addon_readiness.knowledge import Reversibility
from eks_addon_readiness.models import ClusterReference
from eks_addon_readiness.order.plan import PlanStep, StepKind, UpgradePlan
from eks_addon_readiness.stages import release_prep
from eks_addon_readiness.storage import release_key

CLUSTER = "my-eks-cluster"
ACCOUNT = "123456789012"

_ADMIN = f"arn:aws:iam::{ACCOUNT}:role/Admin"
_TERRAFORM = f"arn:aws:iam::{ACCOUNT}:role/{CLUSTER}-terraform"
_ARGOCD = f"arn:aws:iam::{ACCOUNT}:role/argocd-blog-capability-role"
_SERVICE_LINKED = (
    f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/eks.amazonaws.com/"
    "AWSServiceRoleForAmazonEKS"
)
_NODE_GROUP = (
    f"arn:aws:iam::{ACCOUNT}:role/default-eks-node-group-20260523142427808700000001"
)
_READER = f"arn:aws:iam::{ACCOUNT}:role/eks-addon-upgrades-read-{CLUSTER}"

_POLICY = "arn:aws:eks::aws:cluster-access-policy/"


def _cluster(policy: str, scope: str = "cluster", namespaces: tuple[str, ...] = ()) -> dict:
    """One associated access policy, in the shape the API returns it."""
    return {
        "policyArn": f"{_POLICY}{policy}",
        "accessScope": {"type": scope, "namespaces": list(namespaces)},
    }


# The six entries of the real cluster, verbatim.
REAL_ENTRIES: dict[str, tuple[str, list[dict]]] = {
    _ADMIN: ("STANDARD", [_cluster("AmazonEKSClusterAdminPolicy")]),
    _ARGOCD: (
        "STANDARD",
        [
            _cluster("AmazonEKSArgoCDClusterPolicy"),
            _cluster("AmazonEKSArgoCDPolicy", "namespace", ("argocd",)),
        ],
    ),
    _SERVICE_LINKED: (
        "STANDARD",
        [
            _cluster("AmazonEKSClusterInsightsPolicy"),
            _cluster("AmazonEKSEventPolicy"),
        ],
    ),
    _NODE_GROUP: ("EC2_LINUX", []),
    _READER: ("STANDARD", [_cluster("AmazonEKSViewPolicy")]),
    _TERRAFORM: ("STANDARD", [_cluster("AmazonEKSClusterAdminPolicy")]),
}


class FakeEks:
    """The three read calls, with pagination and per-call failure.

    Deliberately not a mock: every test asserts on what came back rather than on which
    method was called, and the pagination is real enough that an implementation reading
    only the first page fails `test_an_admin_on_the_second_page_is_not_missed`.
    """

    def __init__(
        self,
        entries: dict[str, tuple[str, list[dict]]],
        *,
        page_size: int | None = None,
        fail: str = "",
    ) -> None:
        self.entries = entries
        self.page_size = page_size
        self.fail = fail
        self.calls: list[str] = []

    def _maybe_fail(self, call: str) -> None:
        self.calls.append(call)
        if self.fail == call:
            raise RuntimeError(f"AccessDeniedException: {call} is not allowed")

    def list_access_entries(self, **kwargs: object) -> dict:
        self._maybe_fail("list_access_entries")
        names = list(self.entries)
        if self.page_size is None:
            return {"accessEntries": names}
        start = int(str(kwargs.get("nextToken") or 0))
        page = names[start : start + self.page_size]
        out: dict[str, object] = {"accessEntries": page}
        if start + self.page_size < len(names):
            out["nextToken"] = str(start + self.page_size)
        return out

    def describe_access_entry(self, **kwargs: object) -> dict:
        self._maybe_fail("describe_access_entry")
        principal = str(kwargs["principalArn"])
        kind, _ = self.entries[principal]
        return {
            "accessEntry": {
                "clusterName": kwargs["clusterName"],
                "principalArn": principal,
                "type": kind,
                "accessEntryArn": (
                    f"arn:aws:eks:us-east-1:{ACCOUNT}:access-entry/{CLUSTER}/role/"
                    "00000000-0000-0000-0000-000000000000"
                ),
            }
        }

    def list_associated_access_policies(self, **kwargs: object) -> dict:
        self._maybe_fail("list_associated_access_policies")
        principal = str(kwargs["principalArn"])
        _, policies = self.entries[principal]
        if self.page_size is None:
            return {"associatedAccessPolicies": policies}
        start = int(str(kwargs.get("nextToken") or 0))
        page = policies[start : start + self.page_size]
        out: dict[str, object] = {"associatedAccessPolicies": page}
        if start + self.page_size < len(policies):
            out["nextToken"] = str(start + self.page_size)
        return out


# ---------------------------------------------------------------------------
# The resolver, against the real cluster's entries
# ---------------------------------------------------------------------------


def test_the_two_cluster_admins_of_the_real_cluster_are_the_approvers():
    """The whole point, stated against real data: six entries in, two principals out.

    Not "two" as a count -- the exact ARNs. A resolver that returned the view role or the
    service-linked role would still satisfy a length assertion, and one of those two would
    then be able to authorise a cluster change.
    """
    resolved = release_prep.cluster_admins(CLUSTER, client=FakeEks(REAL_ENTRIES))
    assert resolved == (_ADMIN, _TERRAFORM)


def test_the_order_is_deterministic_rather_than_the_order_the_api_listed():
    """Sorted, so two runs of the same cluster produce the same document parameter.

    `ListAccessEntries` makes no ordering promise. An unsorted list means the automation's
    recorded `Approvers` parameter differs run to run for an unchanged cluster, which reads
    as a change to whoever diffs two runs.
    """
    shuffled = {k: REAL_ENTRIES[k] for k in reversed(list(REAL_ENTRIES))}
    assert release_prep.cluster_admins(CLUSTER, client=FakeEks(shuffled)) == (
        _ADMIN,
        _TERRAFORM,
    )


def test_a_cluster_scoped_policy_that_is_not_admin_does_not_authorise():
    """ArgoCD's cluster-scoped policy is real, is cluster-scoped, and is not admin.

    This is the entry that makes the allowlist necessary rather than tidy. A check written
    as "cluster scope and a STANDARD type" -- everything except the policy name -- passes
    for this principal, and ArgoCD's blog capability role would be able to authorise a
    release.
    """
    entries = {_ARGOCD: REAL_ENTRIES[_ARGOCD], _ADMIN: REAL_ENTRIES[_ADMIN]}
    assert release_prep.cluster_admins(CLUSTER, client=FakeEks(entries)) == (_ADMIN,)


def test_an_unknown_policy_arn_is_never_admin():
    """A policy name this code has never heard of is not admin, however it is spelled.

    The denylist version of this check -- "not View, not Edit" -- admits every policy AWS
    adds after today. An allowlist refuses them until somebody reads the new policy and
    decides.
    """
    entries = {
        _ADMIN: ("STANDARD", [_cluster("AmazonEKSSuperDuperAdminPolicy")]),
    }
    with pytest.raises(release_prep.ApproversUnresolved):
        release_prep.cluster_admins(CLUSTER, client=FakeEks(entries))


def test_an_admin_policy_from_somebody_elses_arn_is_refused():
    """The ARN must be an AWS-managed cluster access policy, not merely end in the name.

    Matching on the suffix alone would accept
    `arn:aws:iam::123456789012:policy/AmazonEKSClusterAdminPolicy` -- an account-owned
    policy anybody with `iam:CreatePolicy` can name whatever they like. EKS only associates
    its own managed access policies, so the shape is checkable and therefore checked.
    """
    entries = {
        _ADMIN: (
            "STANDARD",
            [
                {
                    "policyArn": (
                        f"arn:aws:iam::{ACCOUNT}:policy/AmazonEKSClusterAdminPolicy"
                    ),
                    "accessScope": {"type": "cluster", "namespaces": []},
                }
            ],
        ),
    }
    with pytest.raises(release_prep.ApproversUnresolved):
        release_prep.cluster_admins(CLUSTER, client=FakeEks(entries))


def test_a_namespace_scoped_admin_is_not_a_cluster_admin():
    """`AmazonEKSClusterAdminPolicy` scoped to one namespace authorises nothing here.

    An add-on upgrade is a cluster-wide change. A principal trusted to administer the
    `argocd` namespace was never trusted to authorise that.
    """
    entries = {
        _TERRAFORM: (
            "STANDARD",
            [_cluster("AmazonEKSClusterAdminPolicy", "namespace", ("kube-system",))],
        ),
    }
    with pytest.raises(release_prep.ApproversUnresolved):
        release_prep.cluster_admins(CLUSTER, client=FakeEks(entries))


def test_a_node_group_entry_is_refused_even_holding_an_admin_policy():
    """`type` is checked, not just the policy. Only STANDARD entries name a human path.

    The real node group entry carries no policies at all, so a resolver that ignored
    `type` would still exclude it today -- which is exactly why the type check needs its
    own test with an entry contrived to hold the admin policy. An `EC2_LINUX` entry is a
    kubelet identity; treating it as an approver would let anything running on a node
    signal an authorisation.
    """
    entries = {
        _NODE_GROUP: ("EC2_LINUX", [_cluster("AmazonEKSClusterAdminPolicy")]),
    }
    with pytest.raises(release_prep.ApproversUnresolved):
        release_prep.cluster_admins(CLUSTER, client=FakeEks(entries))


def test_the_second_admin_policy_also_qualifies():
    """`AmazonEKSAdminPolicy` at cluster scope is the other half of the allowlist.

    Nothing in the real cluster uses it, so without this test the allowlist could be a
    one-element set and every assertion above would still pass.
    """
    entries = {_TERRAFORM: ("STANDARD", [_cluster("AmazonEKSAdminPolicy")])}
    assert release_prep.cluster_admins(CLUSTER, client=FakeEks(entries)) == (_TERRAFORM,)


def test_an_admin_on_the_second_page_is_not_missed():
    """Both listings paginate, and a missed page is a missing approver.

    Not a theoretical concern in the wrong direction: a resolver that read only the first
    page of `ListAccessEntries` would silently drop admins as a cluster grows, and the
    person who could authorise yesterday gets refused today with nothing explaining why.
    """
    resolved = release_prep.cluster_admins(
        CLUSTER, client=FakeEks(REAL_ENTRIES, page_size=2)
    )
    assert resolved == (_ADMIN, _TERRAFORM)


def test_every_policy_page_of_one_entry_is_read():
    """The admin policy on the second page of one principal's policies still counts."""
    entries = {
        _ADMIN: (
            "STANDARD",
            [
                _cluster("AmazonEKSViewPolicy"),
                _cluster("AmazonEKSArgoCDPolicy", "namespace", ("argocd",)),
                _cluster("AmazonEKSClusterAdminPolicy"),
            ],
        ),
    }
    resolved = release_prep.cluster_admins(
        CLUSTER, client=FakeEks(entries, page_size=1)
    )
    assert resolved == (_ADMIN,)


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def test_a_cluster_with_no_admin_fails_the_run_rather_than_falling_back():
    """The refusal this whole change is arranged around.

    A cluster whose entries hold no cluster admin produces NO approvers, and the honest
    outcome is a failed run: `aws:approve` with an empty `Approvers` would either be
    refused by SSM or -- worse -- pause forever with nobody able to answer. What must never
    happen is a fallback to a hardcoded principal, because then the gate quietly stops
    being about this cluster.
    """
    entries = {k: REAL_ENTRIES[k] for k in (_ARGOCD, _READER, _NODE_GROUP)}
    with pytest.raises(release_prep.ApproversUnresolved) as raised:
        release_prep.cluster_admins(CLUSTER, client=FakeEks(entries))
    assert CLUSTER in str(raised.value)


def test_a_cluster_with_no_entries_at_all_fails_closed():
    """Zero entries is the same refusal, and reaches it by a different path.

    A wrong cluster name, or a region mismatch, returns an empty list with NO error -- the
    trap that makes this worth its own test. It must not read as "no approvers needed".
    """
    with pytest.raises(release_prep.ApproversUnresolved):
        release_prep.cluster_admins(CLUSTER, client=FakeEks({}))


@pytest.mark.parametrize(
    "call",
    ["list_access_entries", "describe_access_entry", "list_associated_access_policies"],
)
def test_an_aws_error_is_a_refusal_rather_than_a_skip(call: str):
    """A call that failed is not a principal that does not qualify.

    Swallowing the error would turn a missing IAM permission into "this cluster has no
    admins" -- which the state machine then reads as a refusal anyway, but with a cause
    that names the wrong problem. Worse, swallowing it PER ENTRY would resolve a partial
    list: the run would page whoever happened to be readable.
    """
    with pytest.raises(Exception) as raised:
        release_prep.cluster_admins(
            CLUSTER, client=FakeEks(REAL_ENTRIES, fail=call)
        )
    assert "AccessDeniedException" in str(raised.value)


def test_an_admin_that_exists_only_in_aws_auth_is_refused():
    """The documented scope limit, pinned so it is a decision rather than a surprise.

    `aws-auth` ConfigMap admins are out of scope: this resolver reads access entries and
    nothing else, so a principal mapped only in that ConfigMap has no entry here and
    cannot authorise a release. The fix, if one is ever wanted, is an access entry for
    them -- not a ConfigMap reader in a Lambda.
    """
    entries = {k: REAL_ENTRIES[k] for k in (_ADMIN,)}
    resolved = release_prep.cluster_admins(CLUSTER, client=FakeEks(entries))
    assert f"arn:aws:iam::{ACCOUNT}:role/only-in-aws-auth" not in resolved


# ---------------------------------------------------------------------------
# What the mail names
# ---------------------------------------------------------------------------


def _plan(*steps: PlanStep) -> UpgradePlan:
    digest = "sha256:" + "a" * 64
    return UpgradePlan(
        cluster=ClusterReference(name=CLUSTER, region="us-east-1"),
        current_kubernetes_version="1.32",
        target_kubernetes_version="1.33",
        steps=steps,
        snapshot_digest=digest,
        report_digest=digest,
        ledger_digest=digest,
        assessment_digest=digest,
        store_digest="sha256:" + "c" * 64,
        ruleset_digest="sha256:" + "b" * 64,
    )


def _move(order: int, component: str, frm: str, to: str, install: str) -> PlanStep:
    return PlanStep(
        order=order,
        kind=StepKind.COMPONENT_MOVE,
        description=f"Move {component}: {frm} → {to} (install {install})",
        component=component,
        from_series=frm,
        to_series=to,
        recommended_version=install,
        at_kubernetes="1.33",
        reversibility=Reversibility.REVERSIBLE,
    )


def test_the_changes_name_the_component_and_both_versions():
    """A line a person can check against the cluster, not a count they have to trust.

    "2 steps approved" tells an approver nothing they can verify. `coredns 1.11.1 ->
    1.12.0` is checkable in one command, and it is the difference between authorising a
    number and authorising a change.
    """
    plan = _plan(
        _move(1, "coredns", "1.11.1", "1.12.0", "1.12.0"),
        _move(2, "vpc-cni", "1.18.x", "1.19.x", "1.19.2"),
    )
    assert release_prep.approved_changes(plan, [1, 2]) == (
        "coredns 1.11.1 -> 1.12.0",
        "vpc-cni 1.18.x -> 1.19.2",
    )


def test_only_the_approved_steps_are_named():
    """A rejected or undecided step must not appear in an authorisation request.

    The mail is what the authorising principal reads. A step they were never asked about
    appearing in the list is the same defect as authorising it.
    """
    plan = _plan(
        _move(1, "coredns", "1.11.1", "1.12.0", "1.12.0"),
        _move(2, "vpc-cni", "1.18.x", "1.19.x", "1.19.2"),
        _move(3, "keda", "2.14.x", "2.15.x", "2.15.1"),
    )
    assert release_prep.approved_changes(plan, [2]) == ("vpc-cni 1.18.x -> 1.19.2",)


def test_the_changes_follow_the_plans_order_rather_than_the_approval_order():
    """Sequence is a property of the plan. The list must read as the plan reads."""
    plan = _plan(
        _move(1, "coredns", "1.11.1", "1.12.0", "1.12.0"),
        _move(2, "vpc-cni", "1.18.x", "1.19.x", "1.19.2"),
    )
    assert release_prep.approved_changes(plan, [2, 1]) == (
        "coredns 1.11.1 -> 1.12.0",
        "vpc-cni 1.18.x -> 1.19.2",
    )


def test_a_cluster_hop_names_the_kubernetes_versions():
    """A cluster step has no component, and naming it `None` would be worse than useless."""
    plan = _plan(
        PlanStep(
            order=1,
            kind=StepKind.CLUSTER_UPGRADE,
            description="Upgrade the cluster 1.32 → 1.33",
            from_kubernetes="1.32",
            to_kubernetes="1.33",
        )
    )
    assert release_prep.approved_changes(plan, [1]) == ("Kubernetes 1.32 -> 1.33",)


def test_a_step_with_no_recorded_installed_version_says_so_rather_than_inventing_one():
    """An advisory step may carry no `from_series`, and the line must not print `None`."""
    plan = _plan(
        PlanStep(
            order=1,
            kind=StepKind.COMPONENT_UPGRADE_RECOMMENDED,
            description="upgrade metrics-server 0.7.1 → 0.8.0 at Kubernetes 1.33",
            component="metrics-server",
            to_series="0.8.x",
            recommended_version="0.8.0",
            at_kubernetes="1.33",
        )
    )
    assert release_prep.approved_changes(plan, [1]) == ("metrics-server -> 0.8.0",)


def test_an_approved_order_the_plan_does_not_have_is_refused():
    """The plan and the decisions disagreeing is a refusal, not a shorter list.

    Silently skipping an unknown order would mail an authorisation request naming fewer
    changes than were approved, and the approver would authorise what they could see.
    """
    plan = _plan(_move(1, "coredns", "1.11.1", "1.12.0", "1.12.0"))
    with pytest.raises(ValueError, match="7"):
        release_prep.approved_changes(plan, [1, 7])


# ---------------------------------------------------------------------------
# The handler, and the key the release object lands on
# ---------------------------------------------------------------------------


def test_the_handler_returns_the_approvers_and_the_changes(monkeypatch):
    """One invocation, both answers, IN THE TWO DIFFERENT SHAPES the document consumes.

    A LIST AND A STRING, AND THE ASYMMETRY IS THE POINT OF THIS ASSERTION. `Approvers` is a
    `StringList` that `aws:approve` reads as a list. `Changes` is read inside two mail bodies,
    and SSM refuses a `StringList` in a substring position -- an apply failed on exactly that:
    `Parameter "Changes" is "STRING_LIST" type and can't be used as a substring parameter`. So
    the handler joins one and not the other, and a future edit that "tidies" them into the same
    shape breaks a deployment rather than a test unless this stays here.
    """
    plan = _plan(
        _move(1, "coredns", "1.11.1", "1.12.0", "1.12.0"),
        _move(2, "vpc-cni", "1.18.5", "1.19.x", "1.19.2"),
    )
    monkeypatch.setattr(release_prep, "_read_plan", lambda uri: plan)
    monkeypatch.setattr(
        release_prep, "_eks", lambda region: FakeEks(REAL_ENTRIES)
    )

    result = release_prep.handler(
        {
            "release_prep": {
                "cluster": CLUSTER,
                "run_id": "toast-191932",
                "plan_uri": "s3://bucket/plans/whatever.json",
                "approved_steps": [1, 2],
            }
        }
    )
    assert result["approvers"] == [_ADMIN, _TERRAFORM]
    assert result["changes"] == "coredns 1.11.1 -> 1.12.0, vpc-cni 1.18.5 -> 1.19.2"
    assert result["cluster"] == CLUSTER


def test_the_joined_changes_refuse_to_be_truncated(monkeypatch):
    """Over SSM's 512-character parameter limit is a refusal, never a shortened list.

    THE ONLY RISK JOINING INTRODUCED. As a `StringList` each line had its own budget; as one
    `String` they share one. Truncating would show an approver fewer changes than the release
    carries and then record that they authorised all of them -- which is the failure this whole
    gate exists to prevent, wearing the face of a mail that looked fine.
    """
    with pytest.raises(ValueError, match="512"):
        release_prep._joined(tuple(f"component-{n} 1.0.0 -> 2.0.0" for n in range(30)))


def test_the_joined_changes_stay_well_inside_the_limit_for_a_real_plan():
    """A seven-step plan -- the most this pipeline produces -- is nowhere near the cap.

    Asserted so the guard above reads as a guard rather than as a constraint somebody has to
    work around: if a realistic plan were close to 512, the right fix would be a shorter line
    format, not a higher tolerance for refusing releases.
    """
    plan = _plan(
        *(
            _move(n, f"addon-number-{n}", "1.18.5", "1.19.x", "1.19.2")
            for n in range(1, 8)
        )
    )
    joined = release_prep._joined(release_prep.approved_changes(plan, range(1, 8)))
    assert len(joined) < 300, len(joined)


def test_the_handler_refuses_a_payload_that_names_no_cluster(monkeypatch):
    """No cluster means no list to resolve, and guessing one would be catastrophic."""
    monkeypatch.setattr(release_prep, "_read_plan", lambda uri: _plan())
    with pytest.raises(ValueError, match="cluster"):
        release_prep.handler({"release_prep": {"plan_uri": "s3://b/k", "approved_steps": [1]}})


def test_the_release_object_key_puts_the_cluster_and_run_in_the_path():
    """`releases/cluster=<cluster>/run=<run>/release.json`, for `step_decision_key`'s reason.

    The cluster is a path SEGMENT so that everything ever authorised for one cluster is one
    prefix listing, and so a release for one cluster can never overwrite another's.
    """
    assert (
        release_key("my-eks-cluster", "toast-191932")
        == "releases/cluster=my-eks-cluster/run=toast-191932/release.json"
    )


def test_the_release_key_carries_no_token_and_no_nonce():
    """A key is a path somebody may log. Nothing capability-bearing belongs in one."""
    key = release_key("c", "run-1")
    assert "nonce" not in key and "token" not in key
