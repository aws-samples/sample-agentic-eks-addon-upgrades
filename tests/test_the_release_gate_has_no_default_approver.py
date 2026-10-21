"""There must be no value of "who may authorise a cluster change" that nobody chose.

WHAT THIS CATCHES, and it is the shape of defect `terraform.tfvars` was written to close
one level down. Every other gate in this module is a variable defaulting to null, so a
defaults-only apply silently un-wires a live stage. The release gate has the mirror-image
risk: a default would silently WIRE one, with an approver list somebody's convenience picked
rather than an operator's decision. A gate whose approvers arrived by default is worse than no
gate, because the run reports that a named principal authorised the release.

WHAT CHANGED, AND WHY THIS FILE IS STRONGER THAN IT WAS. The approver list used to be
`concat([aws_iam_role.release_approver.arn], var.release_approver_arns)` -- assembled at plan
time, with the operator's half committed to `terraform.tfvars`. That satisfied "somebody
chose it" and still had the defect underneath: the thing chosen could disagree with the
cluster's own idea of who administers it, in either direction, silently. So the list is now
RESOLVED PER RUN from the cluster's access entries by `stages/release_prep.py` and passed as a
document parameter at start time.

The invariant asserted here therefore has three parts now:

    1. The document carries NO approver, by default or by literal. `Approvers` is a
       no-default StringList parameter and the `aws:approve` step reads `{{ Approvers }}`.
    2. NOTHING REINTRODUCES A STATIC LIST. No `release_approvers` local, no
       `var.release_approver_arns`, no literal IAM or STS ARN anywhere in the gate.
    3. The value the document receives comes from the RESOLVER, not from Terraform --
       `Approvers.$` reads the `Release approvers` state's output -- and the resolver's refusal
       reaches a Fail state rather than being caught onward.

`Approvers` BEING A REQUIRED INPUT ONLY PROTECTS AGAINST ABSENCE: AWS refuses a start call
that omits it. It does nothing about a WRONG value, and a wrong value is the failure that reads
as success. Hence (2) and (3), which are about where the value comes from rather than whether
there is one.

Deliberately no AWS call: the Terraform is the source, so this runs with no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_GATE = ROOT / "infra/terraform/modules/platform/release_gate.tf"
WORKFLOW = ROOT / "infra/terraform/modules/platform/workflow.tf"
VARIABLES = ROOT / "infra/terraform/modules/platform/variables.tf"
TFVARS = ROOT / "infra/terraform/deploy/terraform.tfvars.example"

# Any literal AWS principal. A role ARN, a user ARN, an assumed-role session ARN.
LITERAL_PRINCIPAL = re.compile(r'"arn:aws[a-z-]*:(?:iam|sts)::')


def _without_comments(text: str) -> str:
    """The HCL with whole-line `#` comments dropped.

    NEEDED BECAUSE THE COMMENTS NAME THE DELETED THINGS ON PURPOSE. `release_gate.tf` explains
    what `local.release_approvers` and `var.release_approver_arns` were and why they are gone --
    that record is the most useful thing in the file for the next reader, and a test that forbade
    the STRINGS rather than the REFERENCES would force it to be deleted.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _variable_block(name: str) -> str:
    """The body of one `variable "<name>" { ... }` block from variables.tf."""
    text = VARIABLES.read_text()
    match = re.search(rf'variable "{re.escape(name)}" \{{(.*?)\n\}}', text, re.S)
    assert match, (
        f'could not find variable "{name}" in variables.tf. If it was renamed, fix this test '
        "rather than deleting it."
    )
    return match.group(1)


def _document_parameter(name: str) -> str:
    """The body of one SSM document parameter block from release_gate.tf."""
    text = RELEASE_GATE.read_text()
    match = re.search(rf"\n      {re.escape(name)} = \{{(.*?)\n      \}}", text, re.S)
    assert match, (
        f"could not find the {name} parameter in the release gate document. The approver list "
        "must be a document parameter, not a baked-in value."
    )
    return match.group(1)


def test_the_approve_step_reads_the_parameter_and_never_a_literal() -> None:
    """`Approvers = "{{ Approvers }}"`, bare, so SSM substitutes the whole StringList.

    A LIST WRAPPING THE PLACEHOLDER IS THE TRAP THIS ALSO CATCHES. `["{{ Approvers }}"]` is
    valid YAML and valid SSM, and it yields ONE element holding a comma-joined string -- a
    pseudo-principal matching nobody, so every approval is refused and the run times out with
    nothing naming the cause.
    """
    text = _without_comments(RELEASE_GATE.read_text())
    # TWO LINES IN THIS FILE BEGIN `Approvers =`: the document's parameter DECLARATION, whose
    # value is a block, and the step's INPUT, which is the one this asserts on. Filtering the
    # block open-brace rather than taking the first match is deliberate -- if the declaration
    # ever stops being a block, this fails loudly instead of silently asserting on the wrong line.
    values = [
        m.group(1).strip()
        for m in re.finditer(r"^\s*Approvers\s*=\s*(.+)$", text, re.M)
        if m.group(1).strip() != "{"
    ]
    assert values, "the aws:approve step declares no Approvers input"
    assert len(values) == 1, (
        f"the release gate document sets Approvers {len(values)} times ({values}). One "
        "`aws:approve` step, one approver input: a second would make which list is in force a "
        "question of step order."
    )
    value = values[0]
    assert value == '"{{ Approvers }}"', (
        f"the aws:approve step names {value} as its Approvers. It must be exactly the bare "
        '"{{ Approvers }}" placeholder: the list is resolved per run from the cluster and '
        "passed at start time, and a bare placeholder is the only form SSM substitutes as a "
        "list."
    )


def test_the_gate_carries_no_static_approver_list() -> None:
    """No local, no variable, no literal. Every route to a plan-time approver list is closed.

    ASSERTED AGAINST THE CODE AND NOT THE PROSE, because the prose names all three on purpose:
    the comments in `release_gate.tf` are where the reason for the deletion is recorded, and a
    test that forbade the strings would force that record out of the file it belongs in.
    """
    text = _without_comments(RELEASE_GATE.read_text())

    found = LITERAL_PRINCIPAL.findall(text)
    assert not found, (
        "the release gate contains a literal IAM or STS ARN. No principal belongs in this "
        "module at all now: the approver list comes from the cluster's access entries at run "
        "time, so a literal here is either a fallback (which must not exist) or somebody "
        "else's account in a published copy of this repository."
    )
    assert not re.search(r"^\s*release_approvers\s*=", text, re.M), (
        "a `release_approvers` local is back in release_gate.tf. The list is resolved per run "
        "by stages/release_prep.py; assembling one at plan time restores the drift this "
        "change removed -- a principal who lost cluster admin keeps authorising releases."
    )
    assert "var.release_approver_arns" not in text, (
        "release_gate.tf reads var.release_approver_arns. That variable was deleted: an "
        "authorisation boundary in a tfvars file can disagree with the cluster's own, in "
        "either direction, with nothing reporting it."
    )


def test_the_variable_is_gone_from_the_module_entirely() -> None:
    """The deletion is the change, so its reversal must fail a test rather than a review.

    Reintroducing `release_approver_arns` would not break anything visibly: the gate would
    still work, mail would still arrive, and a run would still report that a named principal
    authorised the release. It would just be authorising against a list nobody checked against
    the cluster again.
    """
    assert 'variable "release_approver_arns"' not in VARIABLES.read_text(), (
        "variable release_approver_arns is declared again in variables.tf. Who may authorise "
        "a release is the cluster's answer, read from its access entries per run."
    )
    assert not re.search(r"^release_approver_arns\s*=", TFVARS.read_text(), re.M), (
        "release_approver_arns is set in terraform.tfvars. Nothing reads it, so the line "
        "would be a committed authorisation list with no effect -- which is worse than a "
        "wrong one, because a reader would trust it."
    )


def test_the_approvers_parameter_has_no_default() -> None:
    """A document default is exactly how an unchosen approver list gets an answer.

    An SSM parameter default is the value the document supplies when the caller supplies none.
    With one, a `StartAutomationExecution` that resolved nobody -- or that was made by hand --
    would run the gate against whoever the default named.
    """
    body = _document_parameter("Approvers")
    assert not re.search(r"^\s*default\s*=", body, re.M), (
        "the Approvers document parameter declares a default. There must be no approver the "
        "document itself can supply: an unresolvable list fails the run at Release rejected."
    )
    assert '"StringList"' in body, (
        "the Approvers parameter is not a StringList. aws:approve takes a list, and a String "
        "arrives as one comma-joined pseudo-ARN that matches nobody."
    )


def test_the_changes_parameter_has_no_default_either() -> None:
    """What is being authorised must also come from the caller, for the mail's sake.

    A default here would mail an authorisation request naming changes that are not this run's,
    which is a worse failure than naming none: the approver would check the wrong versions
    against the cluster and find them plausible.
    """
    body = _document_parameter("Changes")
    assert not re.search(r"^\s*default\s*=", body, re.M), (
        "the Changes document parameter declares a default. The mail must name this run's "
        "changes or the start call must be refused."
    )


def test_the_start_call_passes_the_resolved_list() -> None:
    """The document's `Approvers` comes from the resolver's output and from nowhere else."""
    text = WORKFLOW.read_text()
    assert '"Approvers.$" = "$.release_prep.approvers"' in text, (
        "the Release gate state does not pass the resolved approver list. The document "
        "parameter has no default, so either the start call fails or -- worse, if a default is "
        "ever added -- the gate runs against a list nobody resolved."
    )
    # WRAPPED IN `States.Array`, unlike `Approvers` beside it, because `Changes` is a `String`
    # holding the joined lines and the SSM Parameters map takes name -> list of strings. Which of
    # the two forms each parameter needs is asserted against its DECLARED type in
    # `test_the_release_gate_document_types_match_its_substitutions.py`; this only pins where the
    # value comes from.
    assert '"Changes.$" = "States.Array($.release_prep.changes)"' in text, (
        "the Release gate state does not pass the per-step change lines, so the mail would "
        "name counts only and an approver could not check what they are authorising."
    )


def test_the_resolver_refusal_reaches_a_fail_state() -> None:
    """An unresolved list must END the run, not be caught onward to the gate.

    This is the fail-closed half of the invariant, and the reason it is asserted in Terraform
    as well as in Python: `stages/release_prep.py` raising is only a refusal if the state
    machine routes the error to `Release rejected`. A catch pointing at `Release gate` would
    turn every resolution failure into an approval request with no approver -- which SSM would
    then refuse at start, in a state whose catch leads to the same place, so the symptom would
    be identical and the diagnosis two states away from the cause.
    """
    text = WORKFLOW.read_text()
    state = re.search(r'"Release approvers" = \{(.*?)\n    \}\n', text, re.S)
    assert state, (
        'no "Release approvers" state in workflow.tf. The approver list is resolved by a '
        "state before the gate; without it the gate has nothing to be passed."
    )
    body = state.group(1)
    # `\s*` around the `=`: `terraform fmt` aligns assignments inside a block, so the literal
    # spacing here is whatever the longest neighbouring key happens to be.
    assert re.search(r'Next\s*=\s*"Release rejected"', body), (
        "the Release approvers state does not route its errors to Release rejected. An empty "
        "resolved list and an AWS error are both refusals of the release, and a refusal that "
        "continues is not a refusal."
    )


def test_the_resolver_scope_variable_has_no_default() -> None:
    """Which clusters the resolver may read is an IAM blast radius, not an approver list.

    Both plausible defaults are traps: `["*"]` restores the `cluster/*` grant the variable
    exists to avoid, and `[]` makes every release fail at the resolve with an IAM error rather
    than a legible one.
    """
    body = _variable_block("release_gate_cluster_names")
    assert not re.search(r"^\s*default\s*=", body, re.M), (
        "release_gate_cluster_names declares a default. A default of `*` would let the "
        "resolver read any cluster's access entries in the account."
    )


def test_the_committed_values_name_the_decisions_that_remain() -> None:
    """The resolver's scope is the ONE cluster this deployment targets, and the user names it.

    The deploy root has no separate release-gate cluster list: `release_gate_cluster_names`
    is wired from the required `target_cluster`, so the cluster the release gate reads admins
    from is always the cluster being assessed and upgraded. The example file must ask for it.
    """
    assert re.search(r"^target_cluster\s*=", TFVARS.read_text(), re.M), (
        "target_cluster is not set in terraform.tfvars.example. It is required, and it is the "
        "release gate's resolver scope."
    )
    main = (ROOT / "infra/terraform/deploy/main.tf").read_text()
    assert re.search(r"release_gate_cluster_names\s*=\s*\[var\.target_cluster\]", main), (
        "the deploy root no longer scopes the release gate to [var.target_cluster]."
    )
