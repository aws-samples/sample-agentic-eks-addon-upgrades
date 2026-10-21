"""Where a paused execution's task token lives while a person decides.

THE PROBLEM THIS SOLVES. The approval stage is a `.waitForTaskToken` state, so Step
Functions hands it a token and stops. The run resumes only when something calls
`SendTaskSuccess` with that token. But the thing that resumes it is the APPROVAL ENDPOINT
-- a Lambda behind a function URL, invoked by a person minutes or hours later, in a
process that never saw the token. So the token has to be written down somewhere both can
reach.

WHY IT IS KEYED BY RUN AND NOT BY CANDIDATE. A run pauses once and resumes once, however
many candidates it queued. Keying by candidate would store the same token three times and
leave three ways to resume the same execution -- and a second `SendTaskSuccess` on a token
already used is an error, so the duplicate would surface as a spurious failure on whichever
approval happened to be second.

WHY THE DECISIONS ARE RECORDED HERE TOO. Resuming is gated on the person pressing Done,
not on a count -- but Done still has to know what it is confirming, and a reader of a
half-finished queue deserves to see which rows they have already decided. The decisions
are held beside the token because they share a lifetime exactly: both exist from the moment
the run pauses until the moment it resumes, and neither is meaningful afterwards.

WHAT THIS IS NOT. It is not the audit trail. The record of who approved what, and the
digest of the page they read, is on the fact itself in `ApprovalRecord` -- durable, and
part of the stored fact. This table is scaffolding for one paused execution and its rows
are expected to expire.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# How long a row outlives the run that wrote it. The execution fails on its own timeout
# well before this, so the TTL is only here to stop abandoned rows accumulating -- it is
# not the approval deadline and must not be read as one.
TTL_DAYS = 7

# How much entropy a link's nonce carries. 32 bytes of `token_urlsafe` is ~43 characters
# and ~256 bits: not guessable, and short enough to survive an email client that decides
# to wrap the URL.
NONCE_BYTES = 32


@dataclass(frozen=True)
class Grant:
    """One thing one link is allowed to do, once.

    THIS IS A CAPABILITY, and naming it that way is the point: whoever holds the nonce can
    perform exactly this action on exactly this candidate. That is why it is scoped as
    narrowly as it is -- an `approve` grant cannot reject, a grant for one candidate cannot
    touch another, and consuming it destroys it.

    `approver` is recorded HERE, when the link is minted, rather than read from whoever
    clicks it. A nonce proves someone opened the mailbox it was sent to; it does not carry
    an identity. So the identity is the address the pipeline sent it to, which is a fact the
    deployment knows and the clicker cannot influence.
    """

    action: str
    approver: str
    candidate_uri: str = ""
    candidate_digest: str = ""
    report_digest: str = ""
    component_id: str = ""
    version_series: str = ""


@dataclass
class PausedRun:
    """One execution waiting on a person, and what they have decided so far."""

    run_id: str
    task_token: str
    report_uri: str
    cluster: str
    # candidate digest -> "approve" | "reject". A rejection counts as a decision: the
    # person looked at it and said no, which is exactly as decided as a yes.
    decisions: dict[str, str] = field(default_factory=dict)
    # Candidate digests the run queued, so Done can report what is still untouched
    # rather than silently resuming past it.
    queued: tuple[str, ...] = ()
    store_digest_before: str = ""
    # nonce -> Grant. Removed as each is used, so the map is also the record of what is
    # still clickable.
    grants: dict[str, Grant] = field(default_factory=dict)

    @property
    def undecided(self) -> tuple[str, ...]:
        return tuple(d for d in self.queued if d not in self.decisions)


def new_nonce() -> str:
    return secrets.token_urlsafe(NONCE_BYTES)


class TokenStore:
    """DynamoDB-backed. One item per paused run."""

    def __init__(self, table_name: str | None = None, client: Any = None) -> None:
        self._table_name = table_name or os.environ.get("APPROVAL_TOKEN_TABLE", "")
        self._client = client

    @property
    def _table(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb")
        return self._client

    def _expiry(self) -> int:
        return int((datetime.now(UTC) + timedelta(days=TTL_DAYS)).timestamp())

    def pause(self, run: PausedRun) -> None:
        """Record a run as waiting. Overwrites, so a retried stage invocation is safe."""
        self._table.put_item(
            TableName=self._table_name,
            Item={
                "run_id": {"S": run.run_id},
                "task_token": {"S": run.task_token},
                "report_uri": {"S": run.report_uri},
                "cluster": {"S": run.cluster},
                "queued": {"SS": list(run.queued)} if run.queued else {"NULL": True},
                "store_digest_before": {"S": run.store_digest_before or "-"},
                "decisions": {"M": {}},
                # Stored as JSON strings rather than nested DynamoDB maps. A grant is
                # read and written whole and never queried by field, so a nested map
                # would buy nothing and cost an attribute-name mapping per field.
                "grants": {
                    "M": {
                        nonce: {"S": json.dumps(grant.__dict__, sort_keys=True)}
                        for nonce, grant in run.grants.items()
                    }
                },
                "expires_at": {"N": str(self._expiry())},
            },
        )

    def get(self, run_id: str) -> PausedRun | None:
        item = self._table.get_item(
            TableName=self._table_name, Key={"run_id": {"S": run_id}}
        ).get("Item")
        if not item:
            return None
        return PausedRun(
            run_id=item["run_id"]["S"],
            task_token=item["task_token"]["S"],
            report_uri=item.get("report_uri", {}).get("S", ""),
            cluster=item.get("cluster", {}).get("S", ""),
            decisions={
                k: v["S"] for k, v in (item.get("decisions", {}).get("M") or {}).items()
            },
            queued=tuple(item.get("queued", {}).get("SS") or ()),
            store_digest_before=item.get("store_digest_before", {}).get("S", ""),
            grants={
                nonce: Grant(**json.loads(value["S"]))
                for nonce, value in (item.get("grants", {}).get("M") or {}).items()
            },
        )

    def consume_grant(self, run_id: str, nonce: str) -> Grant | None:
        """Redeem one link, ONCE. Returns what it authorises, or None if it is spent.

        ATOMIC BY CONDITIONAL DELETE, and that is the whole single-use guarantee. The
        condition and the removal are one DynamoDB call, so two clicks on the same link --
        an impatient double-click, or a mail client that prefetches links, which several do
        -- cannot both succeed. Exactly one gets the grant and the other gets None.

        Doing this as read-then-delete would leave a window where both reads see the grant,
        and the visible symptom would be an approval recorded twice or a
        `SendTaskSuccess` on a token already used. Both are confusing failures a long way
        from their cause.
        """
        stored = self._consume_grant_json(run_id, nonce)
        return Grant(**json.loads(stored)) if stored else None

    def _consume_grant_json(self, run_id: str, nonce: str) -> str | None:
        """The redeem-once mechanics, with no opinion about what a grant looks like.

        EXTRACTED SO A SECOND KIND OF GRANT CAN REUSE THE GUARANTEE RATHER THAN
        RE-IMPLEMENT IT. The upgrade approval stage stores step-shaped grants in its own
        table (`storage/step_tokens.py`), and the subtle part here is not the shape -- it is
        that the condition and the removal are ONE call. A second copy of this written from
        the docstring above would be the obvious place for that to be got wrong, and the
        symptom would be a decision recorded twice with nothing failing.

        Returns the stored JSON, or None when the grant is spent, unknown, or the run is
        gone. All three are the same answer to a click: the decision either already happened
        or was never authorised.
        """
        try:
            response = self._table.update_item(
                TableName=self._table_name,
                Key={"run_id": {"S": run_id}},
                UpdateExpression="REMOVE grants.#n",
                ExpressionAttributeNames={"#n": nonce},
                ConditionExpression="attribute_exists(grants.#n)",
                ReturnValues="ALL_OLD",
            )
        except Exception:
            return None
        return (
            (response.get("Attributes", {}).get("grants", {}).get("M") or {})
            .get(nonce, {})
            .get("S")
        )

    def record_decision(
        self, run_id: str, candidate_digest: str, decision: str
    ) -> None:
        """Note one approve or reject against a paused run.

        A no-op when the run is not paused -- a candidate can legitimately be approved
        outside any run, from the standing queue, and that must not fail.
        """
        try:
            self._table.update_item(
                TableName=self._table_name,
                Key={"run_id": {"S": run_id}},
                UpdateExpression="SET decisions.#d = :v",
                ExpressionAttributeNames={"#d": candidate_digest},
                ExpressionAttributeValues={":v": {"S": decision}},
                ConditionExpression="attribute_exists(run_id)",
            )
        except Exception:
            return

    def find_run_for_candidate(self, candidate_digest: str) -> PausedRun | None:
        """The paused run that queued this candidate, if any.

        A scan, and that is a deliberate choice rather than an oversight: the table holds
        one item per PAUSED run, which is a handful at most, and a GSI on a set attribute
        would cost more to maintain than the scan costs to run. If this table ever holds
        enough rows for that to be wrong, something else has gone wrong first.
        """
        page = self._table.scan(TableName=self._table_name)
        for item in page.get("Items", []):
            if candidate_digest in (item.get("queued", {}).get("SS") or ()):
                return self.get(item["run_id"]["S"])
        return None

    def release(self, run_id: str) -> None:
        """Forget a run, once it has been resumed. Best-effort: the TTL is the backstop."""
        try:
            self._table.delete_item(
                TableName=self._table_name, Key={"run_id": {"S": run_id}}
            )
        except Exception:
            return


def build_token_store() -> TokenStore:
    return TokenStore()
