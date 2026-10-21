"""With `upgrade_stage_enabled = false` the run ends on a readiness report, not an approval.

WHAT HAPPENED BEFORE. The flag gated the lanes and the report Lambda, and nothing else. So a
deployment that would never change a cluster still paused at `Upgrade approval`, mailed a person
"authorisation needed", took their Approve per step, recorded a release -- and reached `Done`
without a further word, because the report state did not exist to send one. A person approved
changes to a cluster and heard nothing back. The approval was theatre and the silence was the bug.

WHAT HAPPENS NOW. `brief_exit` names `Readiness report` when the flag is off: the same function
renders the same page -- plan in order, each step's brief, verdicts, components as scanned, the
digest chain -- with NO control, one sentence saying why, writes it beside where the approval page
would go, mails a notice that asks for nothing, and the run goes to `Done`. Nothing is minted, so
nothing on that page can approve anything. Fact approval upstream is unchanged: facts are fleet
knowledge and need a person whether or not this deployment executes.

Three things pinned: the page carries the notice and no control; the mail asks for nothing; and
the state machine, rendered with the flag off, has `Readiness report -> Done` with the approval and
the release gate absent and nothing unreachable -- and with the flag on, none of that changed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from eks_addon_readiness.notify import build_readiness_email
from eks_addon_readiness.stages import upgrade_approval
from eks_addon_readiness.view import build_document
from eks_addon_readiness.view.render.html import to_html

ROOT = Path(__file__).resolve().parent.parent.parent
PLATFORM = ROOT / "infra" / "terraform" / "modules" / "platform"


class _EmptyStore:
    def find(self, _identity):
        return None

    def digest(self):
        return "UNAVAILABLE"

    def load_errors(self):
        return ()


@pytest.fixture
def document(assessment, plan, inventory, ledger):
    doc, _html, _markdown = build_document(
        inventory=inventory, ledger=ledger, assessment=assessment, plan=plan, store=_EmptyStore()
    )
    return doc


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_notice_renders_and_the_approval_page_is_byte_identical_without_it(document) -> None:
    plain = to_html(document.view, document.provenance())
    told = to_html(
        document.view, document.provenance(), notice="Readiness report. Nothing applies."
    )
    assert '<p class="notice">Readiness report. Nothing applies.</p>' in told
    assert '<p class="notice">' not in plain, (
        "the approval page passes no notice and must not change"
    )
    assert "<form" not in told and "<form" not in plain, (
        "neither read-only render carries a control"
    )


def test_the_readiness_page_has_no_decision_column(document) -> None:
    """With briefs but no forms the sequence table drops the Decision column entirely.

    The readiness report renders the same advice-bearing sequence card the approval page does,
    and it used to keep the sixth column with "No controls rendered / no presigned form for this
    step" on every row -- six repetitions of what the notice at the top said once. No forms at all
    means nothing to decide, so the column goes, and the summary line stops counting approvals.
    """
    from eks_addon_readiness.view.model import AdviceView

    advice = AdviceView(observed_at="2026-09-27T08:00:00+00:00")
    readiness = to_html(document.view, document.provenance(), advice=advice, notice="Readiness.")
    approval = to_html(document.view, document.provenance(), advice=advice, forms={})
    assert "<th>Decision</th>" not in readiness
    assert "No controls rendered" not in readiness
    assert "awaiting your approval" not in readiness
    assert "recommendations, with their briefs" in readiness
    assert "<th>Decision</th>" in approval, "the approval page keeps its column"
    assert 'colspan="6"' not in readiness


def test_the_handler_writes_the_page_mails_and_returns_without_pausing(
    document, monkeypatch, assessment, plan, inventory, ledger
) -> None:
    """Run the real handler over the view fixtures, with storage and mail stubbed at the seams."""
    written: dict[str, str] = {}
    sent: list = []

    monkeypatch.setattr(
        upgrade_approval,
        "_render_inputs",
        lambda payload: (document.view, document.provenance(), None, "", {}, (1, 2)),
    )
    monkeypatch.setattr(
        upgrade_approval,
        "write_text",
        lambda uri, body, _ct: written.__setitem__(uri, body) or uri,
    )
    monkeypatch.setattr(upgrade_approval, "presign_s3_get", lambda uri: f"https://signed/{uri}")
    monkeypatch.setattr(
        upgrade_approval, "send_approval_email", lambda mail: sent.append(mail) or "msg-1"
    )
    monkeypatch.setattr(upgrade_approval, "_components", lambda view, offered: 2)

    result = upgrade_approval.readiness_report(
        {
            "readiness_report": {
                "bucket": "b",
                "cluster_name": "my-eks-cluster",
                "target_kubernetes_version": "1.36",
                "run_id": "run-1",
            }
        }
    )

    (uri,) = written
    assert uri.endswith("upgrade-approvals/cluster=my-eks-cluster/run=run-1/readiness.html")
    page = written[uri]
    assert "does not execute upgrades" in page and "<form" not in page
    assert result["execution_enabled"] is False and result["recommended_steps"] == [1, 2]
    assert result["report_email_sent"] is True
    assert "paused" not in result, "a plain invoke: the run continues to Done"
    (mail,) = sent
    assert "execution disabled" in mail.subject and "Approval needed" not in mail.subject


def test_the_mail_asks_for_nothing() -> None:
    mail = build_readiness_email(
        run_id="r",
        cluster="c",
        target_kubernetes_version="1.36",
        page_url="https://p",
        changes=3,
        components=3,
    )
    assert mail.subject == (
        "EKS readiness: c -> Kubernetes 1.36 "
        "(3 add-on change(s) recommended, execution disabled)"
    )
    for raw in (mail.html_body, mail.text_body):
        body = " ".join(raw.split())
        assert "nothing is asked of you" in body
        assert "approve" not in body.lower().replace("approval", ""), body
        assert "https://p" in body


def test_main_dispatches_the_readiness_key_to_the_report() -> None:
    text = (ROOT / "entrypoints/stages.py").read_text()
    assert '"readiness_report" in event' in text
    assert "upgrade_approval.readiness_report(event, context)" in text


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def _terraform_states(flag: bool) -> dict:
    """Render the definition with the flag set, by asking Terraform for a plan and reading it.

    Skipped without a usable Terraform (or without the AWS provider being able to plan): the
    Python half of this file is what CI runs; this half is what a developer runs before an apply.
    """
    # Check PATH first: subprocess.run raises FileNotFoundError, not a non-zero exit,
    # when the binary is missing (the CI python job has no Terraform).
    if shutil.which("terraform") is None:
        pytest.skip("terraform not on PATH")
    tf = subprocess.run(["terraform", "version"], capture_output=True, text=True)
    if tf.returncode != 0:
        pytest.skip("terraform not usable here")
    out = Path(subprocess.run(["mktemp"], capture_output=True, text=True).stdout.strip())
    plan = subprocess.run(
        [
            "terraform",
            "plan",
            "-no-color",
            "-input=false",
            "-lock=false",
            "-refresh=false",
            f"-var=upgrade_stage_enabled={'true' if flag else 'false'}",
            f"-out={out}",
        ],
        cwd=PLATFORM,
        capture_output=True,
        text=True,
    )
    if plan.returncode not in (0, 2):
        pytest.skip(f"terraform plan unavailable here: {plan.stderr[-300:]}")
    shown = subprocess.run(
        ["terraform", "show", "-json", str(out)], cwd=PLATFORM, capture_output=True, text=True
    )
    data = json.loads(shown.stdout)
    for rc in data.get("resource_changes", []):
        if rc["address"] == "aws_sfn_state_machine.readiness":
            return json.loads(rc["change"]["after"]["definition"])["States"]
    # No change planned: read the definition off the prior state instead.
    for res in (
        data.get("prior_state", {}).get("values", {}).get("root_module", {}).get("resources", [])
    ):
        if res["address"] == "aws_sfn_state_machine.readiness":
            return json.loads(res["values"]["definition"])["States"]
    pytest.skip("no state machine definition in the plan")


def _reachable(states: dict) -> set[str]:
    def successors(state: dict) -> list[str]:
        out = [state[k] for k in ("Next", "Default") if k in state]
        out += [c["Next"] for c in state.get("Choices", [])]
        out += [c["Next"] for c in state.get("Catch", [])]
        return out

    seen: set[str] = set()
    todo = ["Scan"]
    while todo:
        name = todo.pop()
        if name in seen or name not in states:
            continue
        seen.add(name)
        todo += successors(states[name])
    return seen


@pytest.mark.terraform
def test_with_the_flag_off_the_brief_hands_off_to_a_report_and_the_run_ends() -> None:
    states = _terraform_states(flag=False)
    assert states["Readiness report"]["Next"] == "Done"
    assert states["Readiness report"]["Parameters"]["Payload"] == {"readiness_report.$": "$"}
    assert "Upgrade approval" not in states and "Release gate" not in states
    assert set(states) - _reachable(states) == set(), "Step Functions refuses an unreachable state"
    named_by = [k for k, v in states.items() if re.search(r'"Readiness report"', json.dumps(v))]
    assert named_by, "something must hand off to the report"


@pytest.mark.terraform
def test_with_the_flag_on_the_approval_and_the_gate_are_exactly_as_before() -> None:
    states = _terraform_states(flag=True)
    assert "Readiness report" not in states
    assert states["Upgrade approval"]["Next"] == "Anything approved?"
    assert "Release gate" in states and "Upgrade lanes" in states
    assert set(states) - _reachable(states) == set()
