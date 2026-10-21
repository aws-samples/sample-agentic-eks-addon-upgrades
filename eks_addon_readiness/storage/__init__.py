"""Where documents, facts and the marks against facts live.

Three concerns, kept apart because they have different lifetimes and different writers:
stage documents are written once and never modified (`documents.py`, keyed by `keys.py`);
the fact store is long-lived knowledge that stages only ever read and Stage 6 writes
(`facts.py`); and marks say which facts have stopped answering (`marks.py`), written by
the freshness sweep and by a human revoking a fact, so they need no write access to facts
at all.
"""

from .documents import (
    DocumentUriError,
    build_uri,
    list_uris,
    parse_uri,
    read_document,
    read_json,
    read_text,
    write_document,
    write_json,
    write_text,
)
from .facts import (
    DynamoDbFactStore,
    FactWriter,
    build_fact_store,
    build_fact_writer,
    build_raw_fact_store,
    store_entries,
)
from .keys import (
    brief_key,
    candidate_key,
    component_brief_key,
    component_brief_prefix,
    decisions_key,
    dispatch_key,
    gaps_key,
    inventory_key,
    plan_key,
    readiness_report_key,
    release_key,
    report_key,
    run_decisions_key,
    snapshot_key,
    step_decision_key,
    sweep_key,
    upgrade_approval_key,
    upgrade_recorded_key,
)
from .marks import MarkedFactStore, MarkStore, build_mark_store, marked

__all__ = [
    "DocumentUriError",
    "DynamoDbFactStore",
    "FactWriter",
    "MarkStore",
    "MarkedFactStore",
    "brief_key",
    "build_fact_store",
    "build_fact_writer",
    "build_mark_store",
    "build_raw_fact_store",
    "build_uri",
    "candidate_key",
    "component_brief_key",
    "component_brief_prefix",
    "decisions_key",
    "dispatch_key",
    "gaps_key",
    "inventory_key",
    "list_uris",
    "marked",
    "parse_uri",
    "plan_key",
    "read_document",
    "read_json",
    "read_text",
    "readiness_report_key",
    "release_key",
    "report_key",
    "run_decisions_key",
    "snapshot_key",
    "step_decision_key",
    "store_entries",
    "sweep_key",
    "upgrade_approval_key",
    "upgrade_recorded_key",
    "write_document",
    "write_json",
    "write_text",
]
