"""The brief agent: its instruction, its tools, and the schema it must answer in.

**A SEPARATE AGENT FROM THE RESEARCH ONE, deliberately, and the reason is that their
instructions have to say opposite things.** The research agent is told to transcribe and not
to reason past what a page says, at low effort, because the defect it was rebuilt around was a
model turning a tested window into a supported ceiling with perfect arithmetic. This agent is
told to read a CHANGELOG and judge what an operator would care about -- which is reasoning past
the page, on purpose. One agent serving both would have a prompt telling it to be strict and
lenient in the same breath, and the discipline that would erode is the one every stored fact
rests on. Separate Harness, separate ARN, separate prompt, separate ruleset digest, so tuning
this cannot move that.

**IT SEARCHES; IT IS NOT HANDED A PAGE SET.** Release notes are not in one predictable place.
A project may publish them as GitHub Releases, as a CHANGELOG.md, as an UPGRADING.md, as
per-minor upgrade guides on a docs site, or as the body of a release pull request -- and the
same project may change where, halfway through the version range this brief covers. So this
agent gets a tool loop and finds them, and the walk it is given is stated as **coverage it must
account for** rather than as the list of pages to read.

**WHAT MAKES THE OUTPUT WORTH READING IS THE FILTER, AND THE FILTER IS THE PROMPT'S MAIN
JOB.** A release-notes dump is useless to an operator: most entries in most releases are Go
refactors, dependency bumps, CI fixes and docs corrections. The instruction below names what
to keep and what to drop, and `PlatformConcern` gives the keeps a place to go and the drops
none.

**NOTHING HERE DECIDES ANYTHING THE PIPELINE RELIES ON.** The version to move to is already
decided, by an approved fact and the plan. This agent explains a move that has been chosen; it
cannot change it, and it writes no fact. That is what licenses the latitude the instruction
gives it.
"""

from __future__ import annotations

import json
from typing import Any

from ..research.schema import strict_tool
from .boundary import BriefExtraction

# Fields the agent must always supply. The three identity fields plus its own account of what
# it covered -- coverage is compared against the walk, so an answer that omitted it could not
# be checked for completeness.
_REQUIRED_TOP_LEVEL = (
    "component_id",
    "installed_version",
    "recommended_version",
    "releases_read",
    "minors_covered",
    "empty_sections",
)

# Per-object required lists. `concern` and `basis` are here for the reason this module exists:
# a finding that does not say which operator concern it touches, or whether the source said it
# or the reader judged it, cannot be returned at all.
_REQUIRED_BY_OBJECT: dict[str, tuple[str, ...]] = {
    "BriefSource": ("url", "label"),
    "Finding": ("title", "detail", "release", "concern", "basis", "quote", "source"),
    "IssueFinding": ("title", "detail", "concern", "trail", "basis", "quote", "source"),
    "FeatureFinding": (
        "title",
        "detail",
        "release",
        "concern",
        "why_it_helps",
        "enablement",
        "basis",
        "quote",
        "source",
    ),
    "SequencingNote": ("detail", "basis", "quote", "source"),
    "EmptySectionNote": ("section", "outcome", "reason"),
}


def _tighten(schema: dict[str, Any]) -> dict[str, Any]:
    """Close every object and set `required` from the tables above.

    Same walk as `research/schema.py::_tighten`, and the same reason: a model must not be able
    to add a field the deterministic layer does not read, because that is how an unchecked
    value reaches a document. Kept here rather than imported because the required tables are
    this contract's, and sharing the walk while diverging on the tables is how one of the two
    silently stops being enforced.
    """
    if not isinstance(schema, dict):
        return schema

    for defs_key in ("$defs", "definitions", "properties"):
        block = schema.get(defs_key)
        if isinstance(block, dict):
            for name, sub in block.items():
                tightened = _tighten(sub)
                if defs_key in ("$defs", "definitions") and name in _REQUIRED_BY_OBJECT:
                    tightened["required"] = list(_REQUIRED_BY_OBJECT[name])
                block[name] = tightened

    for key in ("items", "additionalItems", "not"):
        if isinstance(schema.get(key), dict):
            schema[key] = _tighten(schema[key])

    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(schema.get(key), list):
            schema[key] = [_tighten(s) for s in schema[key]]

    if schema.get("type") == "object" or "properties" in schema:
        schema["additionalProperties"] = False

    return schema


def extraction_schema() -> dict[str, Any]:
    """The JSON Schema for one brief, closed, with `concern` and `basis` required enums.

    Generated from `BriefExtraction`, so the schema the agent is shown and the contract its
    reply is validated against cannot drift. The doctests are the guarantee, not a
    demonstration:

    >>> s = extraction_schema()
    >>> s["additionalProperties"]
    False
    >>> f = s["$defs"]["Finding"]
    >>> "concern" in f["required"] and "basis" in f["required"] and "quote" in f["required"]
    True
    >>> sorted(s["$defs"]["ChangeBasis"]["enum"])
    ['INFERRED', 'STATED']
    >>> any(k in f["properties"] for k in ("severity", "confidence", "score"))
    False
    >>> "helm_values" in s["$defs"]["FeatureFinding"]["properties"]
    True
    >>> "trail" in s["$defs"]["IssueFinding"]["required"]
    True
    """
    return _tighten(BriefExtraction.model_json_schema()) | {
        "required": list(_REQUIRED_TOP_LEVEL)
    }


SYSTEM_PROMPT = """\
You are a platform engineer with twenty years of experience running Kubernetes in production,
most of it on EKS, and deep familiarity with the CNCF add-on ecosystem: controllers and
operators, cluster and workload autoscalers, ingress and load-balancer controllers, certificate
and secret managers, DNS integrations, metrics and monitoring pipelines, and policy engines.

NO ADD-ON IS NAMED IN THIS INSTRUCTION, DELIBERATELY. You are told which component you are
briefing in the message that follows, and everything you report must come from the sources you
fetch for it. Naming projects here would invite you to answer from what you remember about them,
and what you remember is a training snapshot -- the release you are being asked about may not be
in it, and the release notes you are about to read are authoritative over anything you recall.

A colleague is about to run `helm upgrade` on a live cluster to move ONE add-on from the
version installed today to a newer version. WHICH VERSION TO MOVE TO IS ALREADY DECIDED -- do
not re-litigate it, and do not recommend a different one. Your job is to tell them what they
are walking into, so they can decide whether to do it now, do it in a window, or hold.

You read the project's own sources with your tools and return ONE JSON object.

================================================================================
WHO YOU ARE WRITING FOR
================================================================================

A platform / DevOps / SRE engineer. They own the cluster, the Helm values and the on-call
pager. They are not going to read the source. Write in their terms:

  GOOD  "The `--metric-resolution` flag was removed. If your values file sets it, the pod
         will crash-loop on start. Move the interval to `args.scrapeInterval` before you
         upgrade."
  BAD   "fix(scraper): remove deprecated metric-resolution flag handling (#1204)"

Bullets, not a changelog. Explain the consequence, then what to do. One idea per bullet.

DO NOT REPORT any of these, however prominent in the notes:
  - Go / Rust / internal code refactors, renames, nil-pointer fixes, error-wrapping changes
  - dependency and library bumps, unless the bump itself changes cluster behaviour
  - CI, build, test, lint, release-tooling and container-base-image housekeeping
  - documentation, typo, comment and example changes
  - anything whose effect is invisible from outside the pod

Every finding must map to a `concern` value. If you cannot honestly place it, that is the
signal it does not belong in this brief. Do not reach for OTHER to smuggle one in; if you use
OTHER, justify it in `detail`.

================================================================================
WHAT TO ACTIVELY HUNT FOR -- the things that actually break clusters
================================================================================

Walk EVERY release between installed and recommended and look for each of these. Most are
never called "breaking" by the project and are still the thing that pages someone:

  CRDs             Any CRD added, changed, or with a bumped stored version. Say plainly that
                   `helm upgrade` does NOT apply CRD changes, so it is a manual step -- and
                   whether a rollback can undo it (a stored-version bump cannot be undone by
                   reinstalling the old chart).
  Helm values      A values key renamed, removed, moved or restructured; a chart default
                   changed. This is what an operator hits first and it is usually in the
                   CHART's changelog, not the application's.
  Chart vs app     Which CHART version carries the recommended application version. They are
                   different numbers for most add-ons and operators upgrade charts.
  Defaults         A default that changed with no config change: replicas, resource requests
                   and limits, probe timings, retention, sampling, concurrency, TLS settings.
  Kubernetes       A raised minimum Kubernetes version, a removed API version the add-on
                   uses, or a new one it requires.
  RBAC / identity  New ClusterRole rules, a new ServiceAccount, or a changed IRSA or EKS Pod
                   Identity expectation.
  Metrics          A metric renamed or removed -- dashboards and alerts break silently, which
                   is the worst way to find out.
  Networking       Ports, probes, Service or Ingress shape, webhook endpoints, admission
                   webhook failure policy.
  Availability     Whether the upgrade restarts pods, needs a drain, or has downtime; whether
                   there is an ordering constraint with other add-ons.
  Sequencing       Whether minors may be skipped at all. If the notes say you must upgrade
                   through an intermediate release, that goes in `sequencing` and it is the
                   most important thing in the brief.

================================================================================
WHERE TO LOOK, PER SECTION, IN THIS ORDER
================================================================================

BREAKING_CHANGES
  1. The GitHub release notes for EACH version hop. Look for an explicit heading or wording:
     "BREAKING CHANGE", "BREAKING CHANGES", "Breaking", "Action required", "Upgrade notes",
     "Migration". A finding from one of these is basis=STATED.
  2. If GitHub says nothing, the project's official docs -- release-to-release upgrade notes,
     an UPGRADING.md, a migration guide. GitHub usually has it; the docs are the fallback.
  3. If neither announces any, say so: put BREAKING_CHANGES in empty_sections with
     outcome=NOTHING_ANNOUNCED, and list in looked_in the pages you actually checked. An
     absence is only credible if you can show where you looked.

RELEASE_NOTES
  1. GitHub releases: the features and changes in each hop, including the release pull
     request body where the notes point at one.
  2. Otherwise the official docs' release notes or upgrade notes for those versions.
  Filter hard. This section is what changed that an operator would notice -- not everything
  that changed.

DEPRECATIONS
  Anything deprecated but not yet removed across these releases. This is separate from
  breaking changes on purpose: it does not break this upgrade, and it is what breaks the NEXT
  one. Say which release deprecated it and, if stated, which release will remove it.

KNOWN_ISSUES
  1. Use search_issues on the project's repository. You are looking for issues MANY PEOPLE
     HAVE HIT after moving to these versions -- a long comment trail, many participants,
     reactions, or a maintainer confirming it. One person's broken cluster is not a known
     issue. Put the numbers in `trail`; the tool returns them, so do not estimate.
  2. Also check whether the project or its docs publish a "Known issues" or "Troubleshooting"
     page for these versions.
  3. Prefer issues that are open, recent, and mention one of these versions. Include a
     workaround when the thread has a credible one.

NICE_TO_HAVES
  New capability across these releases that this cluster would actually benefit from -- and
  HOW TO TURN IT ON WITH HELM. `helm_values` is the deliverable: the YAML fragment for a
  values file. If it is on by default, or needs more than Helm (a CRD apply, an IAM role, a
  dependency), say which in `enablement`. A feature named without its enablement is not
  useful to the reader.

================================================================================
RULES CHECKED BY CODE, NOT BY TRUST
================================================================================

  * Every finding carries a QUOTE copied verbatim from the page you read it in, and a SOURCE
    naming that page's url and revision. Every quote is checked byte-for-byte against the
    bytes the tools recorded before you saw them. A composed, paraphrased or reattributed
    quote is reported to the reader as unverified -- it is not silently dropped, so it costs
    the brief its credibility rather than disappearing.
  * Quote the source, then explain in your own words in `detail`. Do not put your explanation
    in the quote.

  * CITE THE EXACT URL YOU FETCHED, not one you could have fetched. The check looks a page up by
    url AND revision together, so citing `.../releases/tags/v1.14.0` after fetching
    `.../releases?per_page=100` finds nothing, and the item is reported unverified even when the
    quote is real. Measured on a live run: 12 items of 101 failed exactly this way. To quote one
    release, FETCH THAT RELEASE -- `https://api.github.com/repos/OWNER/REPO/releases/tags/TAG`
    returns one release, and citing it with revision=TAG matches.

  * QUOTE ONE LINE, NEVER A PARAGRAPH. The releases endpoint returns JSON, and a newline inside
    a JSON string is the two characters backslash-n rather than a line break -- so a quote you
    copy across two lines of rendered prose can never match the recorded bytes. Another 14 items
    of that same run failed this way. One line, copied exactly, matches. If the sentence you want
    spans lines, quote the single line carrying the claim and put the rest in `detail`, which is
    yours to write and is checked against nothing.
  * Attribute each finding to the release whose notes you read it in. Do not merge releases.
  * basis=STATED only when the source itself says it -- a BREAKING CHANGES heading, the words
    "removed", "no longer", "you must", or a named required action. If you concluded it from
    what the entry describes, basis=INFERRED.

    BOTH VALUES ARE EXPECTED, AND ONE OF THEM IS CURRENTLY MISSING. On a live run every one of
    101 items came back STATED, which cannot be right: a changelog line reading "bump
    client-go to v0.33.2" does not say it is breaking, and an item you filed as a breaking
    change on the strength of what that bump IMPLIES is INFERRED. If you reach for STATED
    because it sounds safer, that is the error -- INFERRED is not a weaker answer, it is the
    honest one, and a reader treats the two differently on purpose.

    Reporting a judgement as a statement is the one thing here that damages trust outright.
  * Every empty section must appear in empty_sections with an outcome and a reason.
    NOTHING_ANNOUNCED means you looked and the project announced none. NOT_FOUND means you
    could not find the material. A reader proceeds on the first and holds on the second, so
    never use one for the other.
  * Account for EVERY minor between installed and recommended in minors_covered. If you could
    not find notes for one, do not pretend you covered it -- leave it out, and the document
    will report it as unread.

You are NOT deciding whether to upgrade. A person reads this and decides.
"""


def system_prompt() -> str:
    """The system prompt, WITH the output schema appended.

    **THE SCHEMA GOES IN THE SYSTEM PROMPT, NOT THE USER TURN, and that is a correctness fix
    rather than a tidying.** It was in the user turn, and the Harness truncates with a sliding
    window of `TRUNCATION_MESSAGES_COUNT` messages. This agent's loop is long by design -- a
    release listing, then one fetch per release across the hop, then an issue search, each costing
    a call and a result -- so turn 1 can fall out of the window on exactly the hops this stage
    exists for. The model would then be asked to emit an object whose schema it can no longer see,
    and the visible symptom is a malformed extraction, four re-asks, and a component reported
    NOT_LOOKED for no discoverable reason. A system prompt is never truncated.

    The session id stays in the user turn, because it changes per invocation and a system prompt is
    baked at deploy time. Its instruction is repeated on the tool fields themselves, which is where
    the model reads it at the moment it matters.

    GENERATED, NOT PASTED. `terraform/brief` reads the file this produces, and
    `build-packages.sh` fails the build when that file is stale -- so the deployed schema cannot
    drift from the contract it is generated from. That check was cosmetic while the schema lived in
    the user turn and computed itself fresh each call; moving it here is what makes it load-bearing.
    """
    return (
        SYSTEM_PROMPT
        + "\n"
        + "=" * 80
        + "\nOUTPUT\n"
        + "=" * 80
        + "\n\nReturn ONE JSON object and nothing else -- no prose, no code fence -- matching "
        "this JSON Schema exactly. A field not listed is rejected outright, `concern` and "
        "`basis` are required on every finding, and EVERY empty section must appear in "
        "`empty_sections` with an outcome and a reason.\n\n"
        + json.dumps(extraction_schema(), indent=2, sort_keys=True)
        + "\n"
    )

# Appended when the returned object does not validate. It restates the requirement that was
# violated and carries the validation error, so a near-miss recovers instead of costing the
# component its brief. Same mechanism as the research proxy's, for the same measured reason:
# one shortened field name there cost a whole component its fact.
REASK = (
    "Your previous reply did not match the required output contract. Reply with ONLY one JSON "
    "object matching the schema in your instructions -- no prose, no code fence, and no field "
    "name that is not in the schema. Field names must be spelled exactly as the schema spells "
    "them; a shortened or guessed name is rejected outright. Remember that EVERY empty section "
    "must appear in empty_sections with an outcome of NOTHING_ANNOUNCED or NOT_FOUND and a "
    "reason, and that a finding carrying an action must also carry an action_phase. The "
    "validation error was: "
)


def user_prompt(
    session_id: str,
    component_id: str,
    installed_version: str,
    recommended_version: str,
    *,
    at_kubernetes: str | None = None,
    target_kubernetes: str | None = None,
    repository: str | None = None,
    documentation: tuple[str, ...] = (),
    chart_name: str | None = None,
    releases_on_walk: tuple[str, ...] = (),
    minors_entered: tuple[str, ...] = (),
    as_of_date: str | None = None,
) -> str:
    """The user turn: the hop, the coverage required, where to start, and the schema.

    THE WALK IS STATED AS AN OBLIGATION, NOT AS A PAGE LIST. `releases_on_walk` and
    `minors_entered` are what the agent must account for; how it finds the material is its
    own business, because the material moves between GitHub Releases, a CHANGELOG, an
    UPGRADING.md and a docs site over the range a real hop covers.

    The repository and documentation hosts are stated because they SEED THE FETCH SCOPE --
    the tool refuses a host the component does not declare as its own, so naming them here is
    what makes the first fetch possible rather than a refusal the agent has to discover.

    `as_of_date` is supplied because a model judging release dates against its training cutoff
    calls a current release future-dated, which is the same reason `research/extract.py`
    supplies it.
    """
    lines = [
        f"session_id: {session_id}",
        f"as_of_date: {as_of_date or ''}",
        f"component_id: {component_id}",
        f"installed_version: {installed_version}",
        f"recommended_version: {recommended_version}",
    ]
    if chart_name:
        lines.append(f"helm_chart: {chart_name}")
    if at_kubernetes:
        lines.append(f"kubernetes_version_at_this_step: {at_kubernetes}")
    if target_kubernetes:
        lines.append(f"cluster_target_kubernetes_version: {target_kubernetes}")
    if repository:
        lines.append(f"repository: {repository}")
    if documentation:
        lines.append(f"official_documentation: {', '.join(documentation)}")
    lines.append("")

    lines += [
        f"The cluster runs {component_id} {installed_version} and is moving to "
        f"{recommended_version}. That decision is already made.",
        "",
    ]

    if releases_on_walk:
        lines += [
            f"RELEASES ON THE PATH ({len(releases_on_walk)}): "
            f"{', '.join(releases_on_walk)}",
        ]
    if minors_entered:
        lines += [
            f"MINORS THIS HOP ENTERS: {', '.join(minors_entered)}. You must account for every "
            "one of these in minors_covered. A change introduced in an intermediate release "
            "is still in the version being installed -- reading only the target release's "
            "notes is the single most common way this brief comes out wrong.",
        ]
    lines += [
        "",
        # THE SCHEMA IS NOT REPEATED HERE. It is in the system prompt, which the sliding window
        # cannot evict -- see `system_prompt()`. Repeating it would double the tokens on every
        # call for a copy that adds nothing.
        f"Pass session_id={session_id} on EVERY fetch_source and search_issues call. Without it "
        "the bytes you read cannot be located again and every quote you write is reported to the "
        "reader as unverified.",
        "Return the single JSON object your instructions describe, and nothing else.",
    ]
    return "\n".join(lines)


def tools() -> list[dict[str, Any]]:
    """The three tools this agent gets. All read-only, all recording, deliberately few.

    A model given one way to read a page, one way to search issues and one way to widen its
    scope converges faster than one given eight -- and every tool is a surface that has to be
    argued about. `fetch_source` is the SAME Lambda the research agent calls: fetching must
    stay a tool this system owns, because its first act is to write the bytes down, and that
    recording is the only reason a quote can be checked at all.
    """
    return [
        strict_tool(
            "fetch_source",
            (
                "Fetch one page from the component's own repository or documentation. Use it "
                "for release notes, a CHANGELOG, an UPGRADING or migration guide, a docs "
                "upgrade page, or a release pull request body. Returns the page text. "
                "Refuses any host the component does not declare as its own, and the refusal "
                "explains what is in scope. Every successful fetch is recorded, and your "
                "quotes are checked against those recorded bytes."
            ),
            {
                "url": {
                    "type": "string",
                    "description": (
                        "Absolute https URL. A GitHub blob URL is read as its raw bytes. "
                        "The GitHub API is in scope. Use "
                        "https://api.github.com/repos/OWNER/REPO/releases?per_page=100 to "
                        "ENUMERATE which releases exist, and "
                        "https://api.github.com/repos/OWNER/REPO/releases/tags/TAG to fetch "
                        "the ONE release you intend to quote -- then cite that second URL with "
                        "revision=TAG. Quoting from the listing while citing a per-tag URL is "
                        "the commonest way an item is reported unverified."
                    ),
                },
                "revision": {
                    "type": "string",
                    "description": (
                        "The commit or tag this URL is pinned to, or `issue-<number>` for a "
                        "recorded issue. Empty only for an unversioned documentation page. A "
                        "date is not a revision and is rejected downstream. CITE THE SAME "
                        "revision you fetched with -- the quote check is exact on it."
                    ),
                },
                "canonical_repository": {
                    "type": "string",
                    "description": (
                        "The component's canonical GitHub repository. Supplying it permits "
                        "the hosts that serve that repository; without it only hosts the "
                        "component already declares are readable. It is given to you above."
                    ),
                },
                "official_documentation": {
                    "type": "array",
                    "description": (
                        "Documentation hosts the component declares as its own. Given to you "
                        "above; pass them to read an upgrade guide off a docs site."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "The brief session this fetch belongs to, given to you above. Scopes "
                        "the recorded evidence so the verification layer finds exactly the "
                        "bytes this brief read. OMITTING IT MEANS EVERY QUOTE YOU WRITE IS "
                        "REPORTED UNVERIFIED, because nothing can locate the page again."
                    ),
                },
            },
            ("url", "revision", "session_id"),
        ),
        strict_tool(
            "search_issues",
            (
                "Search the component's GitHub issues for problems many people have hit. "
                "Returns each issue's title, body, url, comment count, reaction count and "
                "dates, so you can judge how widely an issue is hit from numbers rather than "
                "impression. Every result is recorded and citable. IMPORTANT: the result "
                "tells you whether the search actually ran. If it reports that it could not "
                "look -- a rate limit, no token, an invisible repository -- do NOT report an "
                "absence of issues; say the section could not be produced."
            ),
            {
                "repository": {
                    "type": "string",
                    "description": (
                        "The GitHub repository, as `owner/repo` or its full URL."
                    ),
                },
                "search_terms": {
                    "type": "string",
                    "description": (
                        "Optional free text ANDed into the query, e.g. a version number or "
                        "a symptom. Leave empty for all open issues matching the labels."
                    ),
                },
                "labels": {
                    "type": "string",
                    "description": (
                        "Optional comma-separated labels, e.g. `bug` or `bug,regression`. "
                        "Empty searches every label."
                    ),
                },
                "min_comments": {
                    "type": "integer",
                    "description": (
                        "Optional floor on the comment count, for finding issues with a long "
                        "trail. 0 or absent applies no floor."
                    ),
                },
                "state": {
                    "type": "string",
                    "description": "`open`, `closed` or `all`. Prefer `open`.",
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "The brief session this search belongs to, given to you above. Scopes "
                        "the recorded issues so a quote from an issue title or body can be "
                        "verified, and records whether the search ran at all. OMITTING IT "
                        "MEANS EVERY ISSUE QUOTE IS REPORTED UNVERIFIED."
                    ),
                },
            },
            ("repository", "session_id"),
        ),
        strict_tool(
            "register_repository",
            (
                "Declare the component's canonical GitHub repository, which permits fetching "
                "from the hosts that serve it. Only needed when no repository was given to "
                "you above."
            ),
            {
                "repository_url": {
                    "type": "string",
                    "description": "The component's canonical GitHub repository URL.",
                }
            },
            ("repository_url",),
        ),
    ]
