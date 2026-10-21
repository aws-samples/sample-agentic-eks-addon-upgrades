"""Where a paused UPGRADE approval keeps its task token and its per-step capabilities.

Parallel to `storage/tokens.py`, not a reuse of it, and the reason is that one execution now
pauses TWICE. The fact approval stops after `Research`; the upgrade approval stops after the
brief. Both are `.waitForTaskToken` states in the same state machine, so both are handed a
token for the SAME execution -- and `TokenStore` keys its rows on `run_id` alone. One table
and one key space would have the second pause silently overwrite the first, destroying the
token the first one was waiting on.

So this is a second table with a second row shape, and the shapes genuinely differ rather
than being a copy with different names:

    tokens.py       grants reference a CANDIDATE -- a component and a version series, which
                    is a fleet-wide claim, so one approval answers for every cluster
    step_tokens.py  grants reference a STEP of one cluster's plan, which authorises nothing
                    anywhere else

Repurposing `Grant.component_id` to hold a step order would have avoided a file and produced
exactly the homonym defect the `resolve`/`resolve` rename exists to remove.

**WHAT IS REUSED IS THE ONE THING WORTH REUSING.** `TokenStore._consume_grant_json` holds the
redeem-once guarantee: the condition and the removal are a single DynamoDB call, so two
clicks on one link -- an impatient double-click, a mail client that prefetches, and several
do -- cannot both succeed. That is the subtle part, and a second copy written from its
docstring is the obvious place for it to be got wrong. This subclasses to inherit it and
overrides only the item shape.

**THIS IS NOT THE AUDIT TRAIL**, exactly as `tokens.py` says of itself. The durable record of
who decided what, against which plan and which page, is `upgrade.py::StepDecision` in S3.
These rows exist from the moment the run pauses until it resumes and are expected to expire.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .tokens import TTL_DAYS, TokenStore, new_nonce

__all__ = [
    "TTL_DAYS",
    "PausedUpgrade",
    "StepGrant",
    "StepTokenStore",
    "build_step_token_store",
    "new_nonce",
]


@dataclass(frozen=True)
class StepGrant:
    """One thing one form is allowed to do, once, to one step of one cluster's plan.

    A CAPABILITY, scoped as narrowly as the thing it authorises: an approve grant cannot
    reject, a grant for step 3 cannot touch step 5, and consuming it destroys it.

    THE STEP IS DESCRIBED HERE, NOT LOOKED UP LATER. Everything needed to write the durable
    decision is on the grant, because the code that redeems it runs from an S3 event and has
    no plan in front of it. It also means the recorded decision describes the step as the
    PAGE described it -- if the plan is re-sequenced between the mail going out and the click
    arriving, the decision still says what the person was actually shown. Same reasoning as
    `execute/grant.py`'s step fingerprints, at the point the capability is minted.

    `approver` is recorded HERE, when the form is minted, rather than read from whoever
    submits it. A nonce proves someone opened the mailbox it was sent to; it carries no
    identity. So the identity is the address the pipeline mailed, which the deployment knows
    and the submitter cannot influence.
    """

    action: str
    approver: str
    step_order: int = 0
    step_kind: str = ""
    component: str = ""
    installed: str = ""
    recommended: str = ""
    at_kubernetes: str = ""
    # Which brief the page showed for this step, or "" if it had none. Carried so the
    # decision can record that a person approved WITHOUT one, which is a materially
    # different decision from approving having read it.
    brief_observed_at: str = ""


@dataclass
class PausedUpgrade:
    """One execution waiting on a person to decide its upgrade steps.

    `offered` is every step the page showed a control for. It is what makes "left undecided"
    answerable: the per-step decisions in S3 say what was decided, and only the offer says
    what was asked. Without it a reader of six approvals cannot tell whether the seventh was
    rejected or never looked at.
    """

    run_id: str
    task_token: str
    page_uri: str
    cluster: str
    target_kubernetes_version: str = ""
    plan_digest: str = ""
    # The digest of the page bytes actually written. Recorded so every decision can cite
    # WHAT the person read, and so a re-render that changed the page is visible.
    page_digest: str = ""
    offered: tuple[int, ...] = ()
    # step order (as a string, because DynamoDB map keys are strings) -> "approve" | "reject"
    decisions: dict[str, str] = field(default_factory=dict)
    # nonce -> StepGrant. Removed as each is used, so the map is also the record of what is
    # still clickable.
    grants: dict[str, StepGrant] = field(default_factory=dict)

    @property
    def undecided(self) -> tuple[int, ...]:
        """Offered steps nobody has answered yet, ascending."""
        decided = {int(k) for k in self.decisions}
        return tuple(sorted(o for o in self.offered if o not in decided))


class StepTokenStore(TokenStore):
    """DynamoDB-backed. One item per paused upgrade approval.

    Same table schema as `TokenStore` -- `run_id` as the hash key, `expires_at` as the TTL
    attribute -- so the two tables are created by one Terraform module with two names rather
    than two shapes to keep in step.
    """

    def pause_upgrade(self, run: PausedUpgrade) -> None:
        """Record an upgrade approval as waiting. Overwrites, so a stage retry is safe."""
        self._table.put_item(
            TableName=self._table_name,
            Item={
                "run_id": {"S": run.run_id},
                "task_token": {"S": run.task_token},
                "page_uri": {"S": run.page_uri},
                "page_digest": {"S": run.page_digest or "-"},
                "cluster": {"S": run.cluster},
                "target_kubernetes_version": {
                    "S": run.target_kubernetes_version or "-"
                },
                "plan_digest": {"S": run.plan_digest or "-"},
                # A number set, because these are step orders and reading them back as
                # strings would put the int/str conversion in two places.
                "offered": (
                    {"NS": [str(o) for o in run.offered]}
                    if run.offered
                    else {"NULL": True}
                ),
                "decisions": {"M": {}},
                # JSON strings rather than nested maps, for `tokens.py`'s reason: a grant is
                # read and written whole and never queried by field, so a nested map would
                # buy nothing and cost an attribute-name mapping per field.
                "grants": {
                    "M": {
                        nonce: {"S": json.dumps(grant.__dict__, sort_keys=True)}
                        for nonce, grant in run.grants.items()
                    }
                },
                "expires_at": {"N": str(self._expiry())},
            },
        )

    def get_upgrade(self, run_id: str) -> PausedUpgrade | None:
        item = self._table.get_item(
            TableName=self._table_name, Key={"run_id": {"S": run_id}}
        ).get("Item")
        if not item:
            return None
        return PausedUpgrade(
            run_id=item["run_id"]["S"],
            task_token=item["task_token"]["S"],
            page_uri=item.get("page_uri", {}).get("S", ""),
            page_digest=_unset(item.get("page_digest", {}).get("S", "")),
            cluster=item.get("cluster", {}).get("S", ""),
            target_kubernetes_version=_unset(
                item.get("target_kubernetes_version", {}).get("S", "")
            ),
            plan_digest=_unset(item.get("plan_digest", {}).get("S", "")),
            offered=tuple(
                sorted(int(o) for o in (item.get("offered", {}).get("NS") or ()))
            ),
            decisions={
                k: v["S"]
                for k, v in (item.get("decisions", {}).get("M") or {}).items()
            },
            grants={
                nonce: StepGrant(**json.loads(value["S"]))
                for nonce, value in (item.get("grants", {}).get("M") or {}).items()
            },
        )

    def consume_step_grant(self, run_id: str, nonce: str) -> StepGrant | None:
        """Redeem one form, ONCE.

        The mechanics are `TokenStore._consume_grant_json`'s -- one conditional call, so two
        submissions of the same form cannot both succeed. Only the shape of the answer
        differs.
        """
        stored = self._consume_grant_json(run_id, nonce)
        return StepGrant(**json.loads(stored)) if stored else None

    def record_step_decision(self, run_id: str, step_order: int, decision: str) -> None:
        """Note one approve or reject against a paused upgrade.

        A no-op when the run is not paused. Unlike the fact stage, where a candidate can
        legitimately be approved outside any run, an upgrade decision arriving for a run that
        is gone means the window expired -- and the per-step decision object is already in S3
        by the time this is called, so losing this note costs the Done summary a row and
        nothing else.
        """
        try:
            self._table.update_item(
                TableName=self._table_name,
                Key={"run_id": {"S": run_id}},
                UpdateExpression="SET decisions.#d = :v",
                ExpressionAttributeNames={"#d": str(step_order)},
                ExpressionAttributeValues={":v": {"S": decision}},
                ConditionExpression="attribute_exists(run_id)",
            )
        except Exception:
            return


def _unset(value: str) -> str:
    """`""` for the placeholder a required-string attribute is written as.

    DynamoDB refuses an empty string in some contexts and the writer above stores `-` for an
    absent value, so the reader has to undo it. Named rather than inlined because doing it
    in four places invites getting one of them wrong.
    """
    return "" if value == "-" else value


def build_step_token_store() -> StepTokenStore:
    """The store, from the environment.

    A DIFFERENT VARIABLE FROM `APPROVAL_TOKEN_TABLE`, deliberately: pointing both stages at
    one table is the collision this module exists to prevent, and a shared variable name is
    how that would happen by accident.
    """
    return StepTokenStore(os.environ.get("UPGRADE_TOKEN_TABLE", ""))
