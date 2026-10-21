"""The five stages, one module each.

Every stage reads its input document(s) from S3 and writes its output document to
S3. Nothing is passed in memory between stages, and no stage holds state between
invocations — so each one is separately deployable, separately retryable, and its
output is inspectable without running anything.

    1  scan       cluster                      -> snapshot
    2  inventory  snapshot                     -> inventory report
    3  gaps       snapshot, inventory, facts   -> gap ledger
    4  decide     ledger, facts, target        -> readiness assessment
    5  plan       assessment, facts, target    -> upgrade plan
    6  research   a component identity          -> a fact PROPOSED for approval
       approve    a signed human approval       -> the fact, written to the live store

Each stage's document cites the digests of everything upstream of it, so the chain
from a plan back to the cluster reading is walkable and checkable.

Stage 6 is the one place a model runs, and it writes NO fact -- it proposes one. Only
`approve`, behind an IAM-authenticated function URL a person signs, writes to the live
store. Stage 6 is invoked once per unresolved component rather than once per run, so a
slow or unreadable source delays nothing else, and a proposal awaiting approval blocks
nothing: the component stays a typed gap and the run completes.

`freshness` is a state in the machine -- it runs between `Inventory` and `Gaps`, so
its marks land before the fact store is read -- but it is deliberately NOT a stage in the
chain. Two things make that distinction real rather than a label:

- No stage reads its document. It writes marks, and the stages see those through the store
  adapter they already use, so the chain stays a chain of pure functions.
- It is the only part of this system that reads the network, and that is precisely why it
  cannot live inside a stage. A stage document must be a pure function of already-fetched
  evidence; a network read inside Stage 3 would make the gap ledger depend on the instant
  it ran, and two runs would cite the same digests while disagreeing.

It publishes its own document all the same, under `sweeps/`, because "why was this fact
withheld" is a question a person asks and a mark's prose reason is not where they can ask
it. That document's content is time-dependent, honestly so: it is a report on what upstream
looks like now.

`upgrade_brief` is the same shape of thing for the same reason, one step further on. It reads
the Stage 5 plan and writes an ADVISORY document under `briefs/` -- per component move, what
breaks, what open issues to watch for, and what is worth having between the installed and the
recommended version. Three properties make it not a stage:

- No stage reads it. It is delivered beside the upgrade approval, for a person to read.
- Its content is a point-in-time observation. Open issues change daily, so the same plan
  legitimately produces two different briefs an hour apart; the document says
  `reproducible: false` rather than letting a reader assume the digest discipline of the
  assessment and the plan applies here.
- **It cannot fail a run.** Every absence -- no repository, unreadable notes, a rate limit, a
  model that would not answer -- becomes a NOT_LOOKED section carrying its reason, and the
  approval proceeds. That is deliberately the opposite of `approval_gate`, where a run
  legitimately fails because a signature did not arrive.

It reuses Stage 6's fetch-and-record path and its quote verification, and deliberately not
Stage 6's acceptance gate: that gate refuses anything which is not a stated support bound, and
a breaking change is not one. It writes no fact.
"""

from . import (
    approve,
    brief_harness,
    decide,
    fetch_issues,
    fetch_source,
    freshness,
    gaps,
    inventory,
    judge_extraction,
    plan,
    release_prep,
    release_record,
    research,
    resolve,
    scan,
    upgrade_approval,
    upgrade_brief,
    upgrade_decision_watcher,
)

__all__ = [
    "approve",
    "brief_harness",
    "decide",
    "fetch_issues",
    "fetch_source",
    "freshness",
    "gaps",
    "inventory",
    "judge_extraction",
    "plan",
    "release_prep",
    "release_record",
    "research",
    "resolve",
    "scan",
    "upgrade_approval",
    "upgrade_brief",
    "upgrade_decision_watcher",
]
