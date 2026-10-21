"""An object arrived under `upgrade-clicks/`, so a person decided something about a cluster.

Invoked by an S3 event notification, which is the whole point: the browser wrote a small
object with a presigned form, and the ARRIVAL of that object is what reaches code holding
credentials. No public endpoint exists anywhere in this path -- the bucket is private, the
form was signed by the approval stage, and nothing on the internet can invoke anything.

A SECOND WATCHER RATHER THAN A BRANCH IN THE FIRST. `decision_watcher` redeems a fact grant
and invokes `approve`, the only writer of facts. This redeems a STEP grant and writes a
decision about one cluster. Teaching one handler both would have meant one S3 notification
prefix feeding code that has to ask which kind of thing it is holding -- and the failure mode
of getting that wrong is a fact written from an upgrade click.

WHAT THIS HANDLER TRUSTS, WHICH IS ALMOST NOTHING. The object's arrival proves only that
somebody held a valid presigned form. Everything that decides what happens comes from the
GRANT named in the key: the action, the step, its versions, and the approver. The object's
body is never read. So a person who somehow submitted a form for a key they were not given
still gets nothing -- the grant lookup fails and this returns without acting.

IT IS THE ONLY THING HERE THAT MAY RESUME THE RUN, and that is deliberate rather than
incidental. The approval stage holds no `SendTaskSuccess` permission by design, because a
state machine able to resume its own wait makes the human boundary decorative. The permission
belongs on the path a person's decision travels down, which is this handler.

FAILURES ARE SWALLOWED, which is unusual in this codebase and worth stating. An S3 event
notification RETRIES on error, and every operation here is either idempotent already or must
not be repeated -- a retried `SendTaskSuccess` fails on a spent token, and a grant is
single-use so the second attempt finds nothing. Raising would produce a retry storm and a
misleading error metric. What went wrong is logged instead.

ORDER MATTERS AND IT IS S3 FIRST. The durable decision is written BEFORE the token row is
updated and before the run is resumed. A crash between them loses the resume, which a person
can retry; the reverse would lose the decision, which they cannot.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from ..models import ClusterReference
from ..storage import build_uri, run_decisions_key, step_decision_key, write_document
from ..storage.decisions import parse_upgrade_click_key
from ..storage.step_tokens import PausedUpgrade, StepGrant, build_step_token_store
from ..upgrade import Authority, Decision, RunDecisions, StepDecision

BUCKET = os.environ.get("READINESS_BUCKET", "").strip()
REGION = os.environ.get("AWS_REGION", "us-east-1").strip()


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Act on every upgrade click in this notification. Returns what it did, for the log."""
    outcomes: list[dict[str, Any]] = []
    for record in event.get("Records") or []:
        key = ((record.get("s3") or {}).get("object") or {}).get("key") or ""
        # S3 event keys are URL-encoded: `run=a+b` and `%3D` both appear for keys holding
        # spaces or reserved characters. Decoding is not optional -- an undecoded key simply
        # fails to parse and the click is silently dropped.
        outcomes.append(_act(urllib.parse.unquote_plus(key)))
    return {"stage": "upgrade-decision-watcher", "decisions": outcomes}


def _act(key: str) -> dict[str, Any]:
    parsed = parse_upgrade_click_key(key)
    if parsed is None:
        return {"key": key, "acted": False, "reason": "not an upgrade click key"}
    run_id, nonce = parsed

    store = build_step_token_store()
    # READ BEFORE REDEEM, because the paused row carries the cluster, the plan digest and the
    # page digest that every decision has to cite, and redeeming removes only the grant --
    # not the row. Reading after would still work; reading first means a spent nonce costs
    # one call instead of two.
    paused = store.get_upgrade(run_id)
    grant = store.consume_step_grant(run_id, nonce)
    if grant is None:
        # Spent, unknown, or the run is gone. All three are the same answer to a click: the
        # decision either already happened or was never authorised.
        return {
            "key": key,
            "run_id": run_id,
            "acted": False,
            "reason": "no unspent grant for this nonce",
        }
    if paused is None:
        return {
            "key": key,
            "run_id": run_id,
            "action": grant.action,
            "acted": False,
            "reason": "the paused run is gone, so there is nothing to record against",
        }

    if grant.action == "done":
        return _finish(store, paused, grant)
    return _record(store, paused, grant)


def _record(store: Any, paused: PausedUpgrade, grant: StepGrant) -> dict[str, Any]:
    """Write one durable decision, then note it against the paused run.

    S3 FIRST. The decision is the answer; the token row is scaffolding that expires. A crash
    between them leaves a recorded decision the summary will miss, which is recoverable by
    reading the prefix; the reverse would lose the answer itself.
    """
    decision = (
        Decision.APPROVED if grant.action == "approve" else Decision.REJECTED
    )
    if not _complete(paused):
        return {
            "run_id": paused.run_id,
            "step_order": grant.step_order,
            "action": grant.action,
            "acted": False,
            "reason": "the paused run is missing the cluster, plan or page digest",
        }

    record = StepDecision(
        cluster=ClusterReference(name=paused.cluster, region=REGION),
        target_kubernetes_version=paused.target_kubernetes_version,
        plan_digest=paused.plan_digest,
        presented_page_digest=paused.page_digest,
        run_id=paused.run_id,
        step_order=grant.step_order,
        step_kind=grant.step_kind or "COMPONENT_MOVE",
        component=grant.component or "unknown",
        installed=grant.installed,
        recommended=grant.recommended,
        at_kubernetes=grant.at_kubernetes,
        decision=decision,
        approver=grant.approver,
        approver_authority=Authority.EMAIL_LINK_TOKEN,
        decided_at=datetime.now(UTC),
        brief_observed_at=grant.brief_observed_at or None,
    )
    uri = ""
    try:
        uri = write_document(
            build_uri(
                BUCKET,
                step_decision_key(
                    paused.cluster,
                    paused.target_kubernetes_version,
                    paused.plan_digest,
                    paused.run_id,
                    grant.step_order,
                ),
            ),
            record,
        )
    except Exception as exc:
        print(f"upgrade decision could not be written: {type(exc).__name__}: {exc}")
        return {
            "run_id": paused.run_id,
            "step_order": grant.step_order,
            "action": grant.action,
            "acted": False,
            "reason": "the decision object could not be written",
        }

    store.record_step_decision(paused.run_id, grant.step_order, grant.action)
    return {
        "run_id": paused.run_id,
        "step_order": grant.step_order,
        "component": grant.component,
        "action": grant.action,
        "acted": True,
        "decision_uri": uri,
    }


def _finish(store: Any, paused: PausedUpgrade, grant: StepGrant) -> dict[str, Any]:
    """Write the run summary, then resume the execution.

    THE SUMMARY EXISTS FOR ONE FIELD THE PER-STEP OBJECTS CANNOT CARRY: which steps were
    offered and never answered. A reader of six approvals cannot tell whether the seventh was
    rejected or never looked at, and the executor has to tell an answer from a queue.

    It is assembled from the per-step decisions ALREADY IN S3 rather than from the token row's
    map, so the summary and the record cannot disagree about what was decided. The token row
    contributes only the offer.

    RESUMED LAST. A person who pressed Done has finished; if the summary cannot be written,
    failing to resume is the honest outcome -- the run's timeout then reports that a decision
    was needed and something went wrong, rather than the run completing on a summary nobody
    has.
    """
    decisions = _read_decisions(paused)
    summary = RunDecisions(
        cluster=ClusterReference(name=paused.cluster, region=REGION),
        target_kubernetes_version=paused.target_kubernetes_version,
        plan_digest=paused.plan_digest,
        presented_page_digest=paused.page_digest,
        run_id=paused.run_id,
        decisions=decisions,
        offered=tuple(
            sorted(set(paused.offered) - {d.step_order for d in decisions})
        ),
        finished_at=datetime.now(UTC),
    )
    uri = ""
    try:
        uri = write_document(
            build_uri(
                BUCKET,
                run_decisions_key(
                    paused.cluster,
                    paused.target_kubernetes_version,
                    paused.plan_digest,
                    paused.run_id,
                ),
            ),
            summary,
        )
    except Exception as exc:
        print(f"upgrade summary could not be written: {type(exc).__name__}: {exc}")
        return {
            "run_id": paused.run_id,
            "action": "done",
            "acted": False,
            "reason": "the run summary could not be written, so the run was not resumed",
        }

    resumed = _resume(paused, summary)
    if resumed:
        store.release(paused.run_id)
    return {
        "run_id": paused.run_id,
        "action": "done",
        "acted": resumed,
        "summary_uri": uri,
        "approved": list(summary.approved),
        "rejected": list(summary.rejected),
        "undecided": list(summary.offered),
        "detail": summary.summary(),
    }


def _read_decisions(paused: PausedUpgrade) -> tuple[StepDecision, ...]:
    """Every decision already written for this run, read back from S3.

    THE RECORD IS THE SOURCE, not the token row's decision map. The map is a convenience for
    the page; the objects are the answer, and reading them back means the summary is a view of
    what is durably recorded rather than a second claim that could differ from it.

    A step whose object cannot be read is omitted and logged. It then appears as undecided,
    which is the safe direction: offered again next run rather than silently counted as
    approved.
    """
    from ..storage import read_document

    found: list[StepDecision] = []
    for order in paused.offered:
        uri = build_uri(
            BUCKET,
            step_decision_key(
                paused.cluster,
                paused.target_kubernetes_version,
                paused.plan_digest,
                paused.run_id,
                order,
            ),
        )
        try:
            found.append(read_document(uri, StepDecision))
        except Exception:
            continue
    return tuple(found)


def _resume(paused: PausedUpgrade, summary: RunDecisions) -> bool:
    """Hand the execution back to Step Functions. True if it took the token."""
    if not paused.task_token:
        print(f"run {paused.run_id} has no task token, so there is nothing to resume")
        return False
    try:
        import boto3

        boto3.client("stepfunctions").send_task_success(
            taskToken=paused.task_token,
            output=json.dumps(
                {
                    "upgrade_approval": {
                        "run_id": paused.run_id,
                        "cluster": paused.cluster,
                        "approved_steps": list(summary.approved),
                        "rejected_steps": list(summary.rejected),
                        "undecided_steps": list(summary.offered),
                        "summary": summary.summary(),
                    }
                }
            ),
        )
    except Exception as exc:
        print(f"could not resume run {paused.run_id}: {type(exc).__name__}: {exc}")
        return False
    return True


def _complete(paused: PausedUpgrade) -> bool:
    """Whether the paused row carries everything a decision must cite.

    Checked rather than assumed because `StepDecision` refuses a missing plan or page digest
    at construction -- correctly -- and a `ValidationError` inside an S3-notification handler
    becomes a retry storm rather than a legible failure. Better to log the gap once.
    """
    return bool(
        paused.cluster and paused.target_kubernetes_version and paused.plan_digest
        and paused.page_digest
    )
