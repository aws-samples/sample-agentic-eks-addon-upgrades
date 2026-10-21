"""THE acceptance test for Stage 7: two clusters, same add-on, second one is free.

The condition in one line: *"two clusters running
the same add-on. The second must make zero model calls."*

It is tested here against the REAL deterministic gap detector -- `build_report`
(Stage 2) and `build_ledger` (Stage 3) over a real captured snapshot -- rather than
against a hand-written ledger. A hand-written ledger would let the test agree with
itself: the whole claim is that Stage 3's `researchable` set is a pure function of the
inventory, the snapshot and the fleet-wide store, so the store has to be the only
thing that changes between the two clusters.

The two clusters are the same snapshot under two names. That is not a shortcut, it is
the condition: *the same add-on*, in two places, against one shared fact store.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import UTC, datetime

import pytest

from eks_addon_readiness.facts import build_ledger
from eks_addon_readiness.knowledge import ComponentKnowledge
from eks_addon_readiness.loop import (
    DispatchReason,
    NotDispatchedReason,
    NullDispatcher,
    dispatch_queue,
    resume_decision,
)
from eks_addon_readiness.report import build_report
from eks_addon_readiness.scan import snapshot_digest
from eks_addon_readiness.snapshot import ClusterSnapshot
from eks_addon_readiness.storage import dispatch_key
from eks_addon_readiness.storage.facts import _with_aware_refreshed_at

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "snapshots"
REFERENCE = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


class FleetFactStore:
    """A `FactStore` whose contents are controlled by the test.

    Structurally satisfies the Stage 3 `FactStore` Protocol: `find` returns None for
    the unknown case (never raises -- gap detection is *about* the unknown case) and
    `digest` covers every entry, byte-compatible in shape with the real adapters so a
    changed store produces a changed digest.

    This is the fleet: one store, many clusters.
    """

    def __init__(self, entries: dict[str, ComponentKnowledge] | None = None) -> None:
        # Normalised on the way in, exactly as `learn` does and for the same reason:
        # a store is the boundary at which `refreshed_at` becomes aware.
        self._entries = {
            k: _with_aware_refreshed_at(v) for k, v in (entries or {}).items()
        }

    def learn(self, knowledge: ComponentKnowledge) -> None:
        """What an ACCEPTED research result does. The only mutation in the fleet.

        Applies the SAME normalisation the real adapters apply on read
        (`storage.facts._with_aware_refreshed_at`), because that is where the invariant
        lives: a fact enters the system through a store, and every store guarantees an
        aware `refreshed_at` to everything downstream. A double that skipped it would let
        this suite pass over a value the deployed pipeline can never see, and would hide
        the fact that `loop/resolve_loop.py` deliberately carries no coercion of its own.
        """
        self._entries[knowledge.canonical_id] = _with_aware_refreshed_at(knowledge)

    def find(self, identity: str) -> ComponentKnowledge | None:
        return self._entries.get((identity or "").strip())

    def digest(self) -> str:
        import hashlib

        hasher = hashlib.sha256()
        for key in sorted(self._entries):
            hasher.update(key.encode())
            hasher.update(b"\x00")
            hasher.update(self._entries[key].canonical_json().encode())
            hasher.update(b"\x00")
        return "sha256:" + hasher.hexdigest()


def _snapshot_as(cluster_name: str, snapshot_id: str) -> ClusterSnapshot:
    """The captured snapshot, presented as a different cluster.

    Only the cluster name and the snapshot identity change. Every workload, Helm
    release and add-on is identical -- which is what "two clusters running the same
    add-on" means.
    """
    raw = json.loads((FIXTURES / "my-eks-cluster-live.json").read_text())
    raw["cluster"] = {**raw["cluster"], "name": cluster_name}
    raw["snapshot_id"] = snapshot_id
    return ClusterSnapshot.model_validate(raw)


def _ledger_for(snapshot: ClusterSnapshot, store: FleetFactStore):
    """The real Stage 2 and Stage 3 pipeline over one snapshot and one store."""
    digest = snapshot_digest(snapshot)
    report = build_report(snapshot, digest)
    return build_ledger(report, snapshot, store)


# `recorded_entry` comes from `tests/conftest.py`, which AUTHORS the fact rather than
# reading a packaged file store, which this sample does not ship. Its two
# load-bearing properties are unchanged and documented there: the rules cover `0.7.x`,
# `0.8.x` and `0.9.x` and NOT the `0.6.4` the captured snapshot runs (which is why
# `_fact_covering` exists, and the subject of
# `test_a_fact_for_another_series_does_not_answer_the_question`), and `refreshed_at` is
# July, genuinely stale against this module's September `REFERENCE`.


def _fact_covering(
    entry: ComponentKnowledge,
    component_id: str,
    series: str,
    refreshed_at: datetime = REFERENCE,
) -> ComponentKnowledge:
    """The fact a research pass would write for one component at one series.

    Built by re-keying the real recorded entry and pointing its rule at the series
    actually asked about, so the fleet learns something with the shape and the
    evidence citations a promoted research result really has.

    TWO THINGS THIS GETS RIGHT, BOTH LEARNED FROM WATCHING THE TEST FAIL

    **The series.** Stage 3 asks "do we hold a fact for the *installed series*", so
    knowledge is keyed on `(component, series)` and not on the component alone --
    which is exactly why `ResearchRequest` carries both. The packaged entry records
    `0.7.x`/`0.8.x`/`0.9.x` and the captured cluster runs `0.6.4`, so re-keying the
    component without re-pointing the rule leaves the gap wide open. Pinned by
    `test_a_fact_for_another_series_does_not_answer_the_question`.

    **The refresh time.** A research pass stamps `refreshed_at` when it writes, so a
    just-learned fact is fresh by definition. The packaged entry was refreshed in
    July, which is genuinely stale against a September reference -- so reusing its
    timestamp would have the freshness gate immediately re-queue everything the fleet
    had just learned. Correct behaviour, wrong fixture; the default here is the
    instant of learning.
    """
    payload = entry.model_dump()
    payload["canonical_id"] = component_id
    payload["aliases"] = []
    payload["compatibility_rules"] = [
        {**payload["compatibility_rules"][0], "component_version_series": series}
    ]
    payload["refreshed_at"] = refreshed_at
    return ComponentKnowledge.model_validate(payload)


@pytest.fixture
def cluster_a() -> ClusterSnapshot:
    return _snapshot_as("fleet-demo-a", "11111111-1111-4111-8111-111111111111")


@pytest.fixture
def cluster_b() -> ClusterSnapshot:
    return _snapshot_as("fleet-demo-b", "22222222-2222-4222-8222-222222222222")


def test_the_second_cluster_dispatches_nothing_for_a_component_the_fleet_learned(
    cluster_a, cluster_b, recorded_entry
):
    """The acceptance condition, end to end through the real gap detector.

    Cluster A meets an empty fleet: metrics-server has no fact, so it is queued.
    Research accepts a fact and the fleet learns it. Cluster B, running the same
    add-on, finds the question already answered -- so it is NOT queued, and a
    component that is not queued cannot cause a model call.
    """
    fleet = FleetFactStore()

    first = dispatch_queue(_ledger_for(cluster_a, fleet))
    queued_first = {r.component_id for r in first.requests}
    assert "metrics-server" in queued_first
    assert all(r.reason is DispatchReason.FACT_MISSING for r in first.requests)

    # What an ACCEPTED research result does, and the only thing that changes.
    # Learned at the series the first cluster actually asked about.
    asked = next(r for r in first.requests if r.component_id == "metrics-server")
    store_before = fleet.digest()
    fleet.learn(_fact_covering(recorded_entry, "metrics-server", asked.version_series))
    assert fleet.digest() != store_before, "the fleet must observably have learned"

    second = dispatch_queue(_ledger_for(cluster_b, fleet))
    queued_second = {r.component_id for r in second.requests}

    # THE ASSERTION. The second cluster asks nothing about the add-on the first one
    # paid to learn.
    assert "metrics-server" not in queued_second
    assert queued_second == queued_first - {"metrics-server"}


def test_a_fully_known_fleet_dispatches_zero_requests(
    cluster_a, cluster_b, recorded_entry
):
    """The complete version of the claim: zero requests, therefore zero model calls.

    Once the fleet holds a fact for every researchable component, the second cluster
    dispatches an empty queue and says why -- `NOTHING_TO_RESEARCH`, which is
    distinguishable from an unreadable store producing the same length of zero.
    """
    fleet = FleetFactStore()
    first = dispatch_queue(_ledger_for(cluster_a, fleet))
    assert first.requests, "cluster A must have something to research"

    # Every gap the first cluster found is answered, each at the series it asked
    # about. This is the fleet paying once for each question.
    for request in first.requests:
        fleet.learn(
            _fact_covering(recorded_entry, request.component_id, request.version_series)
        )

    second = dispatch_queue(_ledger_for(cluster_b, fleet))
    assert second.requests == ()
    assert second.research_required is False
    assert second.not_dispatched is NotDispatchedReason.NOTHING_TO_RESEARCH

    # And a dispatcher handed that queue makes no call of any kind.
    outcomes = NullDispatcher().research(second.requests)
    assert outcomes == ()


def test_no_model_is_consulted_to_decide_what_to_research():
    """The determinism property, asserted against the import graph.

    Making Stage 7 an agent that decides to research was considered and rejected
    because a model would re-enter the verdict path. The rule modules here must
    therefore reach nothing model-shaped -- checked rather than trusted, because this
    is the one property the whole design rests on.
    """
    import ast

    loop_dir = (
        pathlib.Path(__file__).resolve().parents[2] / "eks_addon_readiness" / "loop"
    )
    forbidden = {"llm", "agent", "strands", "boto3", "bedrock", "research"}
    checked = 0
    for path in sorted(loop_dir.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        checked += 1
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            for name in names:
                head = name.split(".")[0].lstrip(".")
                assert head not in forbidden, (
                    f"{path.name} imports {name!r}. The dispatch decision must be "
                    "deterministic: gap detection, dispatch and resume are pure "
                    "functions of documents, and a model participates only INSIDE a "
                    "dispatched research call."
                )
    assert checked >= 3, "expected the loop package's modules to be present"


def test_a_marked_fact_rejoins_the_queue(cluster_a, recorded_entry):
    """The fleet is not a cache that never expires -- but AGE is not what expires it.

    THIS TEST REPLACES `test_a_stale_fact_rejoins_the_queue`, and the replacement is the
    decision rather than a repair. That test drove a 24-hour window: a fact older than N
    hours re-joined the queue. The project rejected that mechanism, for two reasons
    recorded in `research/freshness.py` -- with every fact human-approved a TTL means
    re-approving every fact every day forever, and it makes one snapshot produce different
    verdicts on different days, which is what the digest chain exists to prevent.

    What replaces it is an upstream release: a sweep outside the pipeline asks the component's
    GitHub repository for its newest release and marks the fact only if that release was
    published after the fact was read. The property this test protects is unchanged -- a
    component whose fact exists is not answered forever -- but the trigger is the component
    shipping something rather than the clock.

    Asserted through the REAL gap detector and the REAL store decorator, so what is proved
    is the whole path: mark -> `find()` returns None -> Stage 3 reports FACT_MISSING ->
    Stage 7 queues it. No part of that path knows what a window is.
    """
    from datetime import timedelta

    from eks_addon_readiness.research.freshness import (
        StaticUpstreamResolver,
        github_repository,
        sweep,
    )
    from eks_addon_readiness.research.marks import MarkKind, MarkLedger
    from eks_addon_readiness.storage.marks import MarkedFactStore

    fleet = FleetFactStore()
    # Installed metrics-server is 0.6.4, so the fact has to cover 0.6.x for the
    # ledger to consider the component answered at all.
    fleet.learn(
        _fact_covering(
            recorded_entry, "metrics-server", "0.6.x", recorded_entry.refreshed_at
        )
    )
    learned = fleet.find("metrics-server")
    assert learned is not None

    repository = github_repository(learned)
    assert repository, "the packaged entry must name a GitHub repository, or nothing decides"
    read_at = learned.refreshed_at

    def _upstream(tag: str, published_at: object) -> StaticUpstreamResolver:
        return StaticUpstreamResolver(
            {repository: {"tag": tag, "published_at": published_at}}
        )

    # NOTHING RELEASED SINCE THE READ: the fact answers, and its age is irrelevant. The
    # reference instant that used to decide this is not passed, because there is nowhere
    # left to pass it.
    quiet = sweep(
        (learned,),
        MarkLedger(),
        REFERENCE,
        resolver=_upstream("v0.6.4", read_at - timedelta(days=30)),
    )
    assert quiet.new_marks == (), "a quiet repository must not withdraw a fact, however old"
    answered = dispatch_queue(_ledger_for(cluster_a, MarkedFactStore(fleet, quiet.ledger)))
    assert "metrics-server" not in {r.component_id for r in answered.requests}

    # A RELEASE AFTER THE READ: the sweep marks it, and the component re-joins the queue --
    # as FACT_MISSING, because from Stage 3's position no source now holds a usable fact.
    drifted = sweep(
        (learned,),
        MarkLedger(),
        REFERENCE,
        resolver=_upstream("v0.7.0", read_at + timedelta(days=1)),
    )
    assert drifted.expired == ("metrics-server",)
    assert drifted.ledger.suppresses("metrics-server").kind is MarkKind.STALE

    withheld = MarkedFactStore(fleet, drifted.ledger)
    assert withheld.find("metrics-server") is None
    # And the store digest moved, so the ledger cannot cite the same store either side.
    assert withheld.digest() != MarkedFactStore(fleet, MarkLedger()).digest()

    requeued = dispatch_queue(_ledger_for(cluster_a, withheld))
    reasons = {r.component_id: r.reason for r in requeued.requests}
    assert reasons["metrics-server"] is DispatchReason.FACT_MISSING


def test_the_queue_is_byte_identical_across_runs(cluster_a, recorded_entry):
    """Determinism, as a document property rather than a claim."""
    learned = _fact_covering(recorded_entry, "metrics-server", "0.6.x")
    fleet = FleetFactStore({learned.canonical_id: learned})
    ledger = _ledger_for(cluster_a, fleet)
    first = dispatch_queue(ledger)
    second = dispatch_queue(ledger)
    assert first.canonical_json() == second.canonical_json()
    assert first.digest() == second.digest()


def test_a_capped_dispatch_takes_the_highest_priority_gaps_first(cluster_a):
    """A cap must not make the queue arbitrary."""
    fleet = FleetFactStore()
    ledger = _ledger_for(cluster_a, fleet)
    full = dispatch_queue(ledger)
    assert len(full.requests) > 1

    capped = dispatch_queue(ledger, max_dispatch=1)
    assert len(capped.requests) == 1
    assert capped.requests[0] == full.requests[0]

    # AND THE CAP REACHES THE KEY, which is the other half of the property. The cap
    # truncates the queue, so two caps are two documents; a key that ignored it would have
    # the second silently overwrite the first. Same defect class as the loop bound in F4
    # below, found in the same function and fixed in the same change.
    assert capped.max_dispatch == 1, "the cap must be recorded as the input it is"
    assert full.max_dispatch is None
    assert capped.digest() != full.digest()

    def key_for(document):
        return dispatch_key(
            "fleet-demo-a",
            document.snapshot_digest,
            document.store_digest,
            document.ruleset_digest,
            "2026-09-01T14:43:02+00:00",
            document.iteration,
            max_iterations=document.max_iterations,
            max_dispatch=document.max_dispatch,
        )

    assert key_for(capped) != key_for(full)
    assert ".cap-1." in key_for(capped)
    assert "cap-" not in key_for(full), "an uncapped run must carry no cap segment"


def test_a_fact_for_another_series_does_not_answer_the_question(
    cluster_a, recorded_entry
):
    """Fleet economics is per (component, installed series), not per component.

    This is what the packaged entry actually demonstrates: it records `0.7.x`,
    `0.8.x` and `0.9.x` for metrics-server, and the captured cluster runs `0.6.4`.
    The ledger therefore reports `NO_RULE_FOR_SERIES` rather than treating the
    component as answered, and the loop still queues it.

    That is the invariant the whole pipeline protects, applied to dispatch: absence
    of knowledge about *this* version is not knowledge. Reusing a neighbouring
    series' fact would be exactly the guess the design refuses to make -- and it
    would be the cheap, wrong way to make the fleet numbers look better.
    """
    fleet = FleetFactStore({recorded_entry.canonical_id: recorded_entry})
    ledger = _ledger_for(cluster_a, fleet)

    entry = next(e for e in ledger.entries if e.identity == "metrics-server")
    assert entry.version_series == "0.6.x"
    assert str(entry.detail_code) == "NO_RULE_FOR_SERIES"
    assert entry.researchable

    queue = dispatch_queue(ledger)
    asked = {r.component_id: r.version_series for r in queue.requests}
    assert asked["metrics-server"] == "0.6.x"


def test_the_loop_refuses_to_resume_on_knowledge_that_was_not_written(
    cluster_a, recorded_entry
):
    """Resume follows the store digest, never what research reported.

    A research stage that reports ACCEPTED while writing nothing must not be able to
    make the loop re-run, because a re-run is how new knowledge reaches a verdict.
    This is the one place a model-facing component could otherwise influence the
    verdict path.
    """
    from eks_addon_readiness.loop import ResearchOutcome

    fleet = FleetFactStore()
    document = dispatch_queue(_ledger_for(cluster_a, fleet))
    assert document.requests

    lying = tuple(
        ResearchOutcome(
            component_id=r.component_id,
            version_series=r.version_series,
            disposition="ACCEPTED",
            detail="claims success",
        )
        for r in document.requests
    )
    # Store digest unchanged: nothing was actually written.
    decision = resume_decision(document, lying, fleet.digest())
    assert decision.resume is False
    assert decision.accepted_reported == len(document.requests)
    assert "No fact was written" in decision.detail

    # And the converse: a real write resumes, whatever the outcomes claim.
    fleet.learn(_fact_covering(recorded_entry, "metrics-server", "0.6.x"))
    honest = tuple(
        ResearchOutcome(
            component_id=r.component_id,
            version_series=r.version_series,
            disposition="FAILED",
            detail="claims failure",
        )
        for r in document.requests
    )
    assert resume_decision(document, honest, fleet.digest()).resume is True


# ---------------------------------------------------------------------------
# THE AMBIENT-INPUT GUARD, and F7 from the independent review.
#
# The first version matched `name.endswith(("datetime.now", ...))` on the UNPARSED callee.
# Walking the AST rather than grepping was the right instinct -- it correctly ignores the
# docstrings that discuss this very defect -- but matching on the unparsed name means
# `from datetime import datetime as dt; dt.now()` unparses to `dt.now` and passes, as does
# `from time import time; time()`. A name-based denylist guarding a property is the weaker
# form, which is exactly what the review said.
#
# It also missed the other ambient-input class in the same stage: `os.environ` reads. That
# is the door F4 came through -- `READINESS_FACT_MAX_AGE_HOURS` reached the document body,
# and therefore its digest, without reaching its key.
#
# So the guard now RESOLVES ALIASES: it binds every import in the module to its origin
# first, then reports the origin of each call. And it covers both classes.
# ---------------------------------------------------------------------------

# Origins that make a document depend on something other than its inputs.
AMBIENT_ORIGINS = {
    "datetime.datetime.now": "the wall clock",
    "datetime.datetime.utcnow": "the wall clock",
    "datetime.datetime.today": "the wall clock",
    "time.time": "the wall clock",
    "time.monotonic": "the wall clock",
    "uuid.uuid4": "a fresh random identifier",
    "random.random": "a random value",
    "os.environ": "the process environment",
    "os.getenv": "the process environment",
    "os.urandom": "a random value",
}


def _import_origins(tree: ast.AST) -> dict[str, str]:
    """Every name bound by an import in this module, mapped to its dotted origin.

    This is the fix for F7. `import datetime` binds `datetime` -> `datetime`;
    `from datetime import datetime as dt` binds `dt` -> `datetime.datetime`;
    `from os import environ` binds `environ` -> `os.environ`. With that map a call can be
    reported by what it IS rather than by what it was called.
    """
    origins: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                origins[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                origins[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return origins


def ambient_inputs(source: str, filename: str = "<test>") -> list[str]:
    """Every ambient input this source reads, as `origin` strings.

    Reports both calls (`dt.now()`) and plain attribute reads (`os.environ["X"]`), because
    the environment is read by subscript rather than by call.
    """
    tree = ast.parse(source, filename=filename)
    origins = _import_origins(tree)

    def resolve(node: ast.AST) -> str | None:
        """The dotted origin of an attribute/name expression, or None."""
        parts: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            return None
        parts.append(origins.get(cur.id, cur.id))
        return ".".join(reversed(parts))

    found: list[str] = []
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Call):
            target = resolve(node.func)
        elif isinstance(node, ast.Attribute):
            target = resolve(node)
        elif isinstance(node, ast.Name):
            target = origins.get(node.id)
        if target and target in AMBIENT_ORIGINS:
            found.append(target)
    return sorted(set(found))


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from datetime import datetime\ndatetime.now()", id="plain"),
        pytest.param("from datetime import datetime as dt\ndt.now()", id="aliased-F7"),
        pytest.param("import datetime as d\nd.datetime.utcnow()", id="aliased-module-F7"),
        pytest.param("from time import time\ntime()", id="bare-time-F7"),
        pytest.param("import os\nos.environ['X']", id="environ-F4"),
        pytest.param("from os import getenv\ngetenv('X')", id="getenv-F4"),
        pytest.param("from uuid import uuid4\nuuid4()", id="uuid"),
    ],
)
def test_the_ambient_guard_catches_every_spelling(source: str) -> None:
    """The aliased and environment spellings are F7. They passed the earlier guard."""
    assert ambient_inputs(source), (
        f"the guard did not catch {source!r}, so a future contributor can reintroduce the "
        "defect it was written for"
    )


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            '"""A docstring about datetime.now() and os.environ, discussed at length."""',
            id="docstring",
        ),
        pytest.param("from datetime import datetime\ndatetime.fromisoformat('x')", id="parse-not-read"),
        pytest.param("import hashlib\nhashlib.sha256(b'x')", id="hashing"),
    ],
)
def test_the_ambient_guard_permits_what_the_loop_legitimately_does(source: str) -> None:
    """`fromisoformat` parses a supplied value; only *reading* an ambient one is the defect."""
    assert not ambient_inputs(source), f"the guard wrongly flagged {source!r}"


def test_no_ambient_input_is_read_anywhere_in_the_dispatch_path():
    """The regression guard for a defect this code actually had.

    The first version of the handler called `datetime.now()` when the payload supplied no
    freshness reference, and recorded that instant in the document -- so two invocations
    over the same ledger produced different bytes at the same S3 key. It was caught by
    re-invoking the deployed function and comparing digests.

    `stages/resolve.py` is deliberately NOT in this sweep: it is the edge, and reading
    `os.environ` there is how a deployment parameter enters at all. The rule is that the
    RULES read nothing ambient -- which is why the sweep is the `loop/` package, whose
    `resolve_loop` module is the one hashed into the dispatch ruleset digest.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "eks_addon_readiness" / "loop"
    checked = 0
    for path in sorted(root.glob("*.py")):
        checked += 1
        found = ambient_inputs(path.read_text(), str(path))
        assert not found, (
            f"{path.name} reads {found}. Staleness is measured against when the cluster "
            "was OBSERVED, and every other input arrives as an argument, so the dispatch "
            "document replays; an ambient read makes two runs over one ledger disagree."
        )
    assert checked >= 3, "expected the loop package's modules to be present"


def test_f1_a_fact_with_no_utc_offset_still_crashes_nothing(cluster_a, recorded_entry):
    """F1, now closed BY CONSTRUCTION rather than by a coercion at the boundary.

    THE ORIGINAL DEFECT. `ComponentKnowledge.refreshed_at` is a bare `datetime`, so a
    writer that omits the offset passes validation. Stage 7's freshness gate subtracted
    that value from an offset-aware reference instant, which raises
    `TypeError: can't subtract offset-naive and offset-aware datetimes` and failed the
    whole execution at `7 Resolve`. It was fixed by normalising `refreshed_at` at the
    store boundary in `storage/facts.py`.

    WHY THE TEST CHANGED SHAPE. Stage 7 no longer subtracts anything: the freshness gate
    is gone, because staleness is upstream drift decided outside the pipeline. There is no
    longer any arithmetic on `refreshed_at` in the dispatch path at all, so the crash is
    not merely prevented -- it is unreachable.

    That is worth asserting rather than deleting, for two reasons. The premise still holds
    (a naive fact is still constructible), and the class of defect moved rather than
    vanished: the remaining consumer of `refreshed_at` is the sweep, which stamps it into
    a mark. So this test now drives a naive fact through BOTH survivors -- a dispatch that
    must not raise, and a mark that must come out aware.

    The boundary normalisation itself is still asserted directly by
    `test_the_store_boundary_is_where_refreshed_at_becomes_aware`.
    """
    naive = ComponentKnowledge.model_validate(
        {
            **recorded_entry.model_dump(),
            "canonical_id": "metrics-server",
            "aliases": [],
            "compatibility_rules": [
                {
                    **recorded_entry.model_dump()["compatibility_rules"][0],
                    "component_version_series": "0.6.x",
                }
            ],
            # THE SPELLING THAT REPRODUCES IT: no offset.
            "refreshed_at": "2026-07-18T00:00:00",
        }
    )
    assert naive.refreshed_at.tzinfo is None, (
        "the schema is expected to accept a naive datetime; if it no longer does, this "
        "test's premise has moved and the coercion may be redundant"
    )

    fleet = FleetFactStore({naive.canonical_id: naive})
    ledger = _ledger_for(cluster_a, fleet)

    # SURVIVOR ONE: dispatch. No instant enters it and none is subtracted, so a naive
    # `refreshed_at` cannot reach any arithmetic. The fact answers; nothing is queued for
    # metrics-server and nothing raises.
    document = dispatch_queue(ledger)
    assert "metrics-server" not in {r.component_id for r in document.requests}

    # SURVIVOR TWO: the mark. The sweep stamps the fact's `refreshed_at` into its mark so
    # the mark still means something after the fact is replaced, and that value must come
    # out offset-aware whatever was stored -- a mark carrying a naive instant would put the
    # same TypeError one layer further out, in whatever later compares two marks.
    from eks_addon_readiness.research.freshness import DriftResult, stale_mark

    mark = stale_mark(
        naive,
        DriftResult(
            stale=True,
            comparable=True,
            repository="kubernetes-sigs/metrics-server",
            release_tag="v0.7.0",
            release_published_at=REFERENCE,
            fact_refreshed_at=REFERENCE,
            url="https://github.com/kubernetes-sigs/metrics-server/releases/tag/v0.7.0",
            reason="the component released after this fact was read",
        ),
        REFERENCE,
    )
    assert mark.fact_refreshed_at is not None
    assert mark.fact_refreshed_at.tzinfo is not None, (
        "the mark must record an aware instant even when the stored fact was naive"
    )

    # SURVIVOR THREE: the comparison itself. `is_stale` reads the naive stored instant
    # directly, so it must normalise before comparing it against an offset-aware release --
    # a TypeError here would be a crashed sweep rather than a verdict.
    from datetime import timedelta

    from eks_addon_readiness.research.freshness import StaticUpstreamResolver, is_stale

    drift = is_stale(
        naive,
        StaticUpstreamResolver(
            {
                "kubernetes-sigs/metrics-server": {
                    "tag": "v0.7.0",
                    "published_at": (
                        naive.refreshed_at + timedelta(days=1)
                    ).isoformat(),
                }
            }
        ),
    )
    assert drift.stale is True
    assert drift.fact_refreshed_at is not None
    assert drift.fact_refreshed_at.tzinfo is not None


def test_f4_a_retuned_loop_bound_does_not_overwrite_the_previous_document(
    cluster_a, recorded_entry
):
    """F4: an input that changes the document's bytes must change its key.

    THE ORIGINAL DEFECT. `freshness_hours` and `max_iterations` were both recorded in the
    document, so both were hashed by `digest()`, and NEITHER reached `dispatch_key`.
    Retuning the freshness window from 24 to 48 and re-running over the same snapshot and
    store therefore wrote DIFFERENT BYTES TO THE SAME KEY, silently replacing the evidence
    `keys.py` claims survives to be compared. Same class as an earlier fix (`datetime.now()` in the
    body but not the key): that fixed the instance, this closed the class.

    ONE OF THE TWO INPUTS IS NOW GONE RATHER THAN KEYED. The freshness window was removed
    with the expiry mechanism itself, so there is no dial left to retune and no segment for
    it in the key. That is a stronger fix than keying it -- an input that does not exist
    cannot fail to reach a key -- but it does leave this test with one variant instead of
    two, which is why it is renamed rather than left implying it still covers both.

    `max_iterations` remains a real question parameter, so the property is still live and
    still asserted: two documents that differ must land on two keys.
    """
    fleet = FleetFactStore(
        {recorded_entry.canonical_id: _fact_covering(recorded_entry, "metrics-server", "0.6.x")}
    )
    ledger = _ledger_for(cluster_a, fleet)

    variants = {
        "default": dispatch_queue(ledger),
        "bound retuned": dispatch_queue(ledger, max_iterations=5),
    }
    digests = {label: d.digest() for label, d in variants.items()}
    keys = {
        label: dispatch_key(
            "fleet-demo-a",
            d.snapshot_digest,
            d.store_digest,
            d.ruleset_digest,
            "2026-09-01T14:43:02+00:00",
            d.iteration,
            max_iterations=d.max_iterations,
        )
        for label, d in variants.items()
    }

    assert len(set(digests.values())) == 2, (
        f"the variants must differ in content, or this test proves nothing: {digests}"
    )
    assert len(set(keys.values())) == 2, (
        "two different documents landed on one key, so one silently overwrote the "
        "other:\n  " + "\n  ".join(f"{k}: {v}" for k, v in keys.items())
    )
    # And the removed dial leaves no trace in the key it used to be missing from.
    assert all("fresh-" not in key for key in keys.values())


def test_the_store_boundary_is_where_refreshed_at_becomes_aware(recorded_entry):
    """The invariant, asserted directly on the function that establishes it.

    `ComponentKnowledge.refreshed_at` is a bare `datetime` in a digest-frozen module, so
    the field cannot be tightened without moving Stage 3's ruleset digest. The invariant
    is therefore established where a fact is READ, which is the one place every consumer
    goes through.
    """
    naive = ComponentKnowledge.model_validate(
        {**recorded_entry.model_dump(), "refreshed_at": "2026-07-18T00:00:00"}
    )
    assert naive.refreshed_at.tzinfo is None, "premise: the schema accepts a naive value"

    normalised = _with_aware_refreshed_at(naive)
    assert normalised.refreshed_at.tzinfo is UTC
    assert normalised.refreshed_at.isoformat() == "2026-07-18T00:00:00+00:00"

    # An ALREADY-aware fact must come back untouched, byte for byte -- this is what keeps
    # the deployed store digest from moving.
    aware = _with_aware_refreshed_at(recorded_entry)
    assert aware.canonical_json() == recorded_entry.canonical_json()


def _is_aware(knowledge: ComponentKnowledge) -> bool:
    """The one predicate both tests below apply, so neither can drift from the other."""
    return knowledge.refreshed_at.tzinfo is not None


def test_the_aware_guarantee_is_not_vacuous(synthetic_entry_payload):
    """Proves the check above still has teeth now that it iterates a possibly-empty tree.

    Without this, `test_every_packaged_knowledge_entry_is_already_aware` would pass on an
    empty directory for the same reason it passes on a good one, and a naive entry committed
    later would be caught by nothing. So the SAME predicate is applied to an entry authored
    to be naive, and must reject it.
    """
    naive = ComponentKnowledge.model_validate(
        {**synthetic_entry_payload, "refreshed_at": "2026-07-18T00:00:00"}
    )
    assert naive.refreshed_at.tzinfo is None, "premise: the schema accepts a naive value"
    assert not _is_aware(naive), "the guard must reject a naive entry"

    assert _is_aware(ComponentKnowledge.model_validate(synthetic_entry_payload)), (
        "and must accept the committed shape, or it would reject every valid entry"
    )
