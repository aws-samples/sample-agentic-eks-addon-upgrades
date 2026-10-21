"""`entrypoints/stages.py`'s transition table is a claim about the deployed machine. Hold it to that.

WHY THIS TEST EXISTS. `entrypoints/stages.py`'s module docstring carries a table headed "the deployed
state machine ... State names are the ones in the console". That is not commentary, it is
an assertion about live infrastructure, and it is the first thing a reader opens. It has
now gone stale three times in a row and each time silently:

  * `Sweep` was inserted between `Inventory` and `Gaps` and the table never gained it.
  * `Resolve` moved from behind `Plan` to directly behind `Gaps` and the table kept the
    old edges, so it showed `Plan -> Resolve` for a machine that runs `Gaps -> Resolve`.
  * every state name lost its ordinal and the table kept `1 Scan` ... `7 Resolve`, which
    are no longer the names in the console and cannot be found there.

A prose table cannot be trusted to be updated by whoever changes the graph -- that is
exactly what did not happen three times -- so the table is parsed and asserted here.

WHAT THIS TEST IS NOT. It is not a second source of truth for the pipeline's shape. The
expected edges below are transcribed from ONE `describe-state-machine` read of the live
machine, cited by date and ARN, and this test's whole job is to fail when the docstring
stops matching that. If the machine's shape is changed deliberately, this test is
SUPPOSED to fail: update the docstring and the block below together, from a fresh
`describe-state-machine`, and the diff then shows both halves moving at once.

Deliberately no AWS call: this must run in the standard suite with no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

# Transcribed from the deployed definition, read read-only with
#   aws stepfunctions describe-state-machine --region us-east-1 \
#     --state-machine-arn arn:aws:states:us-east-1:123456789012:stateMachine:eks-addon-upgrades
# 17 states, StartAt Scan, from a single read that also produced the edges below.
#
# SEVERAL VALUES BELOW ARE AHEAD OF THE LIVE MACHINE, AND THAT IS STATED RATHER THAN HIDDEN.
# Two changes have landed in the Terraform and neither is applied:
#
#   1. The report state was removed. `Present` left the state set and `Done` became the single
#      terminal state. Verified against `terraform plan` on that commit.
#   2. `Upgrade approval` was added, and the brief's three exits -- `Brief plan`'s catch and
#      `Brief assemble`'s Next and catch -- now name it instead of `Done`, which the approval
#      names in turn.
#   3. THE RELEASE GATE'S SEVEN STATES were added, and `Upgrade approval` now names
#      `Anything approved?` rather than `Done`. Transcribed from the HCL for reason (2)'s
#      reason and one more: `Release gate` and `Release polled` are `aws-sdk:ssm` Tasks whose
#      SSM document does not exist until apply, so Terraform reports the whole definition as
#      unknown and a plan cannot render these edges either.
#   4. THE APPROVAL PRE-CHECK was added: `Approval survey` and `Anything to approve?`, and BOTH
#      edges that used to enter `Approval` -- `Research.Next` and the `approval_pending` branch
#      of `Research needed?` -- now route through them. Transcribed from the HCL and NOT YET
#      APPLIED; the live definition still has `Research.Next = "Approval"`.
#      Re-verify against a fresh `describe-state-machine` after the apply.
#
#      That same read corrected this constant rather than only adding to it: the live
#      `Research needed?` already carried an `approval_pending -> Approval` branch that neither
#      this set nor entrypoints/stages.py's table had ever named, so both described a two-branch Choice for a
#      three-branch machine. The edge is now present, pointing at the pre-check.
#
# The second could NOT be read from a plan at all: the state's `Resource` names a Lambda that
# does not exist yet, so Terraform reports the whole definition as unknown until apply. Those
# values are therefore transcribed from the HCL rather than from any rendered definition, which
# is weaker evidence than the rest of this constant and is why it is said here. Re-verify the
# whole set against a fresh `describe-state-machine` once the machine is updated; the states
# not named in this comment are still the live read.
DEPLOYED_EDGES = {
    ("Scan", "Inventory"),
    ("Inventory", "Sweep"),
    ("Sweep", "Gaps"),
    ("Gaps", "Resolve"),
    ("Resolve", "Decide"),
    ("Decide", "Plan"),
    ("Plan", "Research needed?"),
    ("Research needed?", "Research"),
    # THE `approval_pending` BRANCH, AND IT WAS MISSING FROM THIS SET BEFORE THE PRE-CHECK
    # LANDED. `Research needed?` has had a second condition on the live definition since the
    # stranded-candidate fix -- confirmed by `describe-state-machine`, whose
    # `Research needed?` carries `And [IsPresent $.approval_pending, BooleanEquals true] ->
    # Approval`. Neither this constant nor entrypoints/stages.py's table named it, so both described a
    # two-branch Choice for a machine that has three. It now names the pre-check, which is what
    # the branch points at.
    ("Research needed?", "Approval survey"),
    ("Research needed?", "Anything to upgrade?"),
    # THE PRE-CHECK, AND ITS CATCH EDGE IS HERE for the brief's reason. The survey is an
    # optimisation over behaviour that already worked, so every way it can fail lands on the wait
    # -- which is what the run would have done without it. An edge that exists only on failure is
    # still an edge, and this one is the difference between "counts first" and "can break a run".
    ("Research", "Approval survey"),
    ("Approval survey", "Anything to approve?"),
    ("Approval survey", "Approval"),
    # THE CHOICE THAT SPENDS THE COUNT. Its Default is the WAIT, not the skip: only a counted
    # zero goes to `Knowledge changed?`, so an absent or non-numeric count still asks a person.
    ("Anything to approve?", "Approval"),
    ("Anything to approve?", "Knowledge changed?"),
    ("Approval", "Knowledge changed?"),
    ("Knowledge changed?", "Gaps"),
    ("Knowledge changed?", "Anything to upgrade?"),
    # The upgrade brief. THE CATCH EDGES ARE HERE TOO, unlike every edge above, because they
    # are the load-bearing ones: the brief is advisory and must not be able to fail a run, so
    # `Brief plan` and `Brief assemble` catching to `Done` and the Map catching to
    # `Brief assemble` are the property a reader most needs to see in this table. An edge that
    # only exists on failure is still an edge.
    ("Anything to upgrade?", "Brief plan"),
    ("Brief plan", "Any to research?"),
    ("Brief plan", "Upgrade approval"),
    # WITH `upgrade_stage_enabled` FALSE, `brief_exit` NAMES `Readiness report` INSTEAD, and the
    # approval and the release gate are not emitted at all: a deployment that executes nothing
    # renders the same page with no control, mails it, and ends. Both `brief_exit` targets are in
    # this table for the reason `Release recorded`'s two edges are: an edge that exists only under
    # a condition is still an edge, and a reader needs to see which condition.
    ("Brief plan", "Readiness report"),
    ("Brief assemble", "Readiness report"),
    ("Readiness report", "Done"),
    ("Any to research?", "Brief research"),
    ("Any to research?", "Brief assemble"),
    ("Brief research", "Brief assemble"),
    ("Brief assemble", "Upgrade approval"),
    # The second wait. Reached from all three of the brief's exits, including both catches, so
    # a brief that could not be produced still gets a person asked.
    ("Upgrade approval", "Anything approved?"),
    # THE RELEASE GATE. Ten states, and the CATCH EDGES ARE HERE for the brief's reason
    # inverted: the brief's catches matter because they lead ONWARD, and these matter because
    # they must not. Every way `Release approvers`, `Release gate` and `Release polled` can fail
    # lands on `Release rejected`, which is a Fail -- an unauthorised release must never reach
    # `Done`.
    ("Anything approved?", "Release approvers"),
    ("Anything approved?", "Done"),
    # WHO MAY AUTHORISE, ASKED OF THE CLUSTER. Its catch edge is the one that carries the
    # fail-closed property: an empty resolved list, or an AWS error reading the access entries,
    # refuses the release rather than proceeding with no approver.
    ("Release approvers", "Release gate"),
    ("Release approvers", "Release rejected"),
    ("Release gate", "Release waiting"),
    ("Release gate", "Release rejected"),
    ("Release waiting", "Release polled"),
    ("Release polled", "Release decided?"),
    ("Release polled", "Release rejected"),
    ("Release decided?", "Release approved"),
    ("Release decided?", "Release rejected"),
    # The poll loop closes here. `aws:approve`'s own timeout is what bounds it, so this edge
    # cannot spin forever even though nothing counts the iterations.
    ("Release decided?", "Release waiting"),
    # THE DOCUMENT IS BUILT BEFORE IT IS WRITTEN, in a Lambda, because ASL cannot decode the JSON
    # text SSM emits nor read the plan that says what step 4 moves. Neither edge has a catch: by
    # here somebody HAS approved, so a failure must fail the run rather than claim nobody did.
    ("Release approved", "Release assembled"),
    ("Release assembled", "Release recorded"),
    # THE UPGRADE STAGE HANGS OFF `Release recorded`, so that state has TWO outgoing edges: the
    # stage is gated on `upgrade_stage_enabled`, which DEFAULTS TO FALSE, and the live machine takes
    # the `Done` edge until an operator names `upgrade-stage.tfvars` on a plan. Both are in this
    # table for the reason the brief's catch edges are: an edge that exists only under a condition
    # is still an edge, and a reader needs to see which condition.
    #
    # IT FOLLOWS THE RECORD RATHER THAN THE APPROVAL, which is a property worth reading off this
    # table: a lane opens only after the authorisation it acts on is durable in S3.
    #
    # TRANSCRIBED FROM THE HCL, not from a `describe-state-machine`, and more weakly than the
    # release gate's edges were: these states are not applied anywhere. `Upgrade lane` is a Map
    # whose Iterator states are nested and therefore excluded below, and `Lane upgrade`'s Resource
    # names a CodeBuild project that does not exist until apply, so Terraform reports the whole
    # definition as unknown and a plan cannot render these edges either.
    ("Release recorded", "Done"),
    ("Release recorded", "Upgrade lanes"),
    ("Upgrade lanes", "Upgrade lane"),
    ("Upgrade lanes", "Upgrade report"),
    ("Upgrade lane", "Upgrade report"),
    ("Upgrade report", "Anything referred?"),
    ("Anything referred?", "Done"),
    ("Anything referred?", "Upgrade referred"),
}

DEPLOYED_STATES = {
    "Scan",
    "Inventory",
    "Sweep",
    "Gaps",
    "Resolve",
    "Decide",
    "Plan",
    "Research needed?",
    "Research",
    "Approval survey",
    "Anything to approve?",
    "Approval",
    "Knowledge changed?",
    "Anything to upgrade?",
    "Brief plan",
    "Any to research?",
    "Brief research",
    "Brief assemble",
    "Upgrade approval",
    # Present INSTEAD of the approval and the release gate when `upgrade_stage_enabled` is
    # false; listed with them for the reason `Upgrade lanes` is listed beside `Done`.
    "Readiness report",
    "Anything approved?",
    "Release approvers",
    "Release gate",
    "Release waiting",
    "Release polled",
    "Release decided?",
    "Release approved",
    "Release assembled",
    "Release recorded",
    "Release rejected",
    "Upgrade lanes",
    "Upgrade lane",
    "Upgrade report",
    "Anything referred?",
    "Upgrade referred",
    "Done",
}

TABLE_HEADING = "The deployed state machine, by transition."


def _docstring() -> str:
    """`entrypoints/stages.py`'s module docstring, read as text rather than imported.

    Read from the file instead of `import main` on purpose: importing pulls in every
    stage package and its dependencies to read a docstring, and this test is about the
    bytes a reader sees in the file.
    """
    source = (Path(__file__).resolve().parent.parent / "entrypoints/stages.py").read_text()
    match = re.match(r'\s*"""(.*?)"""', source, re.S)
    assert match, "entrypoints/stages.py does not open with a module docstring"
    return match.group(1)


def _transition_table() -> list[str]:
    """The lines of the transition table, from its heading to the following blank line."""
    doc = _docstring()
    assert TABLE_HEADING in doc, (
        f"entrypoints/stages.py's docstring no longer contains the heading {TABLE_HEADING!r}. "
        "If the table was renamed, update TABLE_HEADING here in the same change."
    )
    # Drop the REST OF THE HEADING'S OWN LINE, not just the heading text. Splitting on the
    # heading alone leaves the tail of that same line ("State names are the ones in the
    # console:") as the first line of the table, and since the table proper starts after a
    # blank line the scan below then stopped on that blank and returned the tail as the
    # whole table. Every edge assertion still failed, but `no ordinals` PASSED against a
    # table it had never read -- a green test proving nothing, which is worse here than a
    # red one. Anchor to the end of the line instead.
    after = doc.split(TABLE_HEADING, 1)[1].split("\n", 1)[1]
    lines: list[str] = []
    for line in after.splitlines():
        if not line.strip():
            if lines:  # the blank line that ends the table, not the one before it
                break
            continue
        lines.append(line)
    assert lines, "the transition table heading is present but the table under it is empty"
    assert any("->" in line for line in lines), (
        "the block under the transition heading contains no `->` at all, so this test is "
        f"not looking at a transition table. It read: {lines!r}"
    )
    return lines


def _parse_edges(lines: list[str]) -> set[tuple[str, str]]:
    """Every `A -> B` in the table, carrying `A` down across continuation lines.

    A continuation line -- one that starts with `->` because its source is the same as
    the line above -- is how the two Choice states express their branches. Splitting the
    destination on two-or-more spaces is what separates it from the trailing condition
    (`if research_required`), so a name with a single space in it (`Research needed?`)
    survives and a condition never becomes part of a state name.
    """
    edges: set[tuple[str, str]] = set()
    source = ""
    for line in lines:
        if "->" not in line:
            continue
        left, right = line.split("->", 1)
        if left.strip():
            source = left.strip()
        assert source, f"a continuation line appeared before any source state: {line!r}"
        destination = re.split(r"\s{2,}", right.strip())[0].strip()
        edges.add((source, destination))
    return edges


# ---------------------------------------------------------------------------
# The anchor that makes this file more than two hand-written constants agreeing.
#
# EVERY DRIFT THIS TEST EXISTS TO CATCH ORIGINATED IN `workflow.tf`. Sweep was inserted
# there, Resolve was moved there, the ordinals were dropped there. So comparing entrypoints/stages.py's
# table to `DEPLOYED_EDGES` above -- a second constant, transcribed by hand, in the same
# repository -- catches a change to entrypoints/stages.py and MISSES a fourth change to the graph: both
# halves would go stale together and the suite would stay green. That is the failure mode
# this whole file is about, reproduced one level up.
#
# So the STATE NAMES are derived from the HCL instead. `workflow.tf` and `sweep.tf` are the
# only places a state is declared, and a state declaration is the sole use of the
# `"Name" = {` form at the top of a state map. Add a state there and this test fails until
# entrypoints/stages.py's table names it.
#
# The EDGES still come from the transcribed constant, deliberately: successors in the HCL are
# conditional expressions (`var.resolve_function_arn == null ? "Decide" : "Resolve"`), so
# deriving them would mean evaluating Terraform, and the tfstate that holds the resolved
# answer is gitignored. Names are unconditional and edges are not, so each is taken from the
# source that can actually answer for it.
HCL_STATE_SOURCES = (
    "infra/terraform/modules/platform/workflow.tf",
    "infra/terraform/modules/platform/sweep.tf",
    # The upgrade stage declares its five top-level states in its own file, for the reason that
    # file's header gives: another lane is applying `workflow.tf`, so this stage's footprint
    # there is
    # two lines. Listed here so adding a state to it still fails this test until entrypoints/stages.py's table
    # names it -- which is the whole mechanism this file exists to provide.
    "infra/terraform/modules/platform/upgrade_stage.tf",
)

# Declared in the HCL and deliberately NOT expected in entrypoints/stages.py's table:
HCL_STATES_NOT_IN_THE_DEPLOYED_MACHINE = {
    # A nested state inside the `Research` Map's Iterator, not a state of the top-level
    # machine. `check_run.py` may report it for a running execution, which is why the
    # system prompt mentions it, but it is not an edge in the pipeline's graph.
    "Research one component",
    # The same, inside the `Brief research` Map's Iterator, plus the Pass state its per-item
    # Catch routes to, so one failed brief becomes an `unbriefed` result.
    "Brief one component",
    "Brief failed",
    # The four states inside the `Upgrade lane` Map's Iterator. Nested, so they are not states of
    # the top-level machine and cannot be pasted into the console and found -- exactly as with the
    # two above.
    #
    # THEY ARE NOT UNTESTED FOR BEING EXCLUDED HERE.
    # `tests/test_the_upgrade_lane_always_closes_its_access_entry.py` walks this exact set out
    # of the
    # HCL and proves that no path through it reaches an end without deleting the access entry, which
    # is a stronger claim than a table of names could carry.
    "Lane open",
    # CLUSTER-ADMIN, ASSOCIATED TO THE ENTRY AND REVOKED WITH IT. Its own state because the entry has
    # to exist before a policy can be associated to it, and because a failure here must still reach
    # the cleanup -- by that point the entry is already live.
    "Lane grant",
    # The agent's read-only entry, minted after the write grant and before the build.
    "Lane open reader",
    "Lane upgrade",
    # TWO CLEANUPS, one per access entry, because two SDK calls cannot be one state. The reader's
    # comes off first and the writer's last, so the grant that could change the cluster is the one
    # still being retried if only one delete gets through.
    "Lane cleanup",
    "Lane cleanup writer",
    "Lane outcome",
}

# `Done` USED TO BE EXCLUDED HERE and is not any more. It was emitted only when
# `present_function_arn` was null -- a terminal Pass for a Choice's Default to name -- and
# `terraform.tfvars` committed that ARN, so it never reached the machine. The report state
# is gone, `done_state` is no longer conditional on it, and `Done` is now the machine's single
# terminal state, so it belongs in the table like any other.


def _states_declared_in_hcl() -> set[str]:
    """Every state name the Terraform declares, minus the two documented exclusions."""
    root = Path(__file__).resolve().parent.parent
    declared: set[str] = set()
    for relative in HCL_STATE_SOURCES:
        path = root / relative
        assert path.exists(), (
            f"{relative} is gone. It is one of the two files this test derives the state "
            "names from; if the workflow moved, update HCL_STATE_SOURCES."
        )
        declared |= set(re.findall(r'^\s+"([A-Z][A-Za-z ?]*)" = \{', path.read_text(), re.M))
    assert declared, (
        "found no state declarations in "
        f"{HCL_STATE_SOURCES}. The `\"Name\" = {{` form this test matches has probably "
        "changed shape -- fix the pattern rather than deleting the test."
    )
    return declared - HCL_STATES_NOT_IN_THE_DEPLOYED_MACHINE


def test_the_transcribed_edges_still_agree_with_the_terraform() -> None:
    """Guards `DEPLOYED_EDGES` itself, so the two constants cannot go stale together.

    Without this, a state added to `workflow.tf` would leave both this file's constant and
    entrypoints/stages.py's table describing a machine that no longer exists, with the suite green.
    """
    transcribed = {state for edge in DEPLOYED_EDGES for state in edge}
    declared = _states_declared_in_hcl()
    assert transcribed == declared, (
        "DEPLOYED_EDGES no longer matches the states declared in Terraform.\n"
        f"declared in HCL, missing from DEPLOYED_EDGES: {sorted(declared - transcribed)}\n"
        f"in DEPLOYED_EDGES, not declared in HCL: {sorted(transcribed - declared)}\n"
        "If the graph changed on purpose: re-read the live definition with "
        "describe-state-machine, update DEPLOYED_EDGES and entrypoints/stages.py's table together, and "
        "this test plus the ones below will confirm all three agree."
    )


def test_the_table_names_exactly_the_states_the_terraform_declares() -> None:
    """entrypoints/stages.py's table against the HCL directly, not against the constant above."""
    named = {state for edge in _parse_edges(_transition_table()) for state in edge}
    declared = _states_declared_in_hcl()
    assert named == declared, (
        f"declared in Terraform, missing from entrypoints/stages.py's table: {sorted(declared - named)}\n"
        f"in entrypoints/stages.py's table, not declared in Terraform: {sorted(named - declared)}"
    )


def test_the_table_carries_no_ordinals_in_any_state_name() -> None:
    """No `1 Scan` or `7 Resolve`. The console has no such names to look up.

    This is the assertion that fails loudest on a regression, because reintroducing an
    ordinal is how all three previous staleness events looked.
    """
    offenders = [line for line in _transition_table() if re.search(r"\b[1-9]\s+[A-Z]", line)]
    assert not offenders, (
        "entrypoints/stages.py's transition table names states with ordinals, which the deployed "
        "machine does not have:\n  " + "\n  ".join(line.strip() for line in offenders)
    )


def test_the_table_names_every_deployed_state_and_invents_none() -> None:
    """The table's state names are exactly the deployed machine's eleven."""
    edges = _parse_edges(_transition_table())
    named = {state for edge in edges for state in edge}
    assert named == DEPLOYED_STATES, (
        f"missing from the table: {sorted(DEPLOYED_STATES - named)}\n"
        f"in the table but not deployed: {sorted(named - DEPLOYED_STATES)}"
    )


def test_the_table_shows_sweep_between_inventory_and_gaps() -> None:
    """The stage the table has twice been written without.

    Called out separately from the edge-set comparison so a failure names the actual
    problem rather than a diff of twelve tuples.
    """
    edges = _parse_edges(_transition_table())
    assert ("Inventory", "Sweep") in edges, "the table does not run Inventory into Sweep"
    assert ("Sweep", "Gaps") in edges, "the table does not run Sweep into Gaps"


def test_the_table_shows_resolve_directly_after_gaps() -> None:
    """The reorder: `Gaps -> Resolve -> Decide`, never `Plan -> Resolve`."""
    edges = _parse_edges(_transition_table())
    assert ("Gaps", "Resolve") in edges, "the table does not run Gaps into Resolve"
    assert ("Resolve", "Decide") in edges, "the table does not run Resolve into Decide"
    assert ("Plan", "Resolve") not in edges, (
        "the table still shows the pre-reorder edge Plan -> Resolve"
    )
    assert ("Gaps", "Decide") not in edges, (
        "the table still shows the pre-reorder edge Gaps -> Decide"
    )


def test_the_table_matches_the_deployed_edges_exactly() -> None:
    """The whole graph, so an edge cannot drift while the individual checks above pass."""
    edges = _parse_edges(_transition_table())
    assert edges == DEPLOYED_EDGES, (
        f"edges the deployed machine has and the table does not: "
        f"{sorted(DEPLOYED_EDGES - edges)}\n"
        f"edges the table claims and the machine does not have: {sorted(edges - DEPLOYED_EDGES)}"
    )


def test_the_docstring_does_not_claim_resolve_reads_an_assessment() -> None:
    """Resolve runs BEFORE Decide, so no assessment exists when it runs.

    `infra/terraform/modules/platform/workflow.tf` says so at the `Gaps` successor: resolve reads
    `event["ledger_uri"]` and `event["snapshot_captured_at"]` "and nothing else -- no
    assessment, no plan, no digest of either", and `stages/resolve.py`'s own header is
    `in    a gap ledger`. A docstring naming the assessment as an input describes a
    document that does not exist at that point in the run.
    """
    doc = _docstring()
    resolve_rows = [
        line
        for line in doc.splitlines()
        if re.search(r"\bresolve\b", line) and "->" in line and "assessment" in line
    ]
    assert not resolve_rows, (
        "entrypoints/stages.py's stage table gives resolve an assessment as an input, but resolve runs "
        "before decide and no assessment exists yet:\n  "
        + "\n  ".join(line.strip() for line in resolve_rows)
    )
