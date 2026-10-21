"""The report as one self-contained HTML document.

A pure function of `(ReportView, Provenance)`. Imports `..model` and the standard
library -- no stage document, no fact store, and since approval moved upstream, no
approval controls either. A test enforces it.
That is what makes the single-source-of-truth guarantee checkable rather than
aspirational: with the value set as the only input, a renderer has nothing to
re-derive a number *from*.

DESIGN NOTES THAT ARE LOAD-BEARING, NOT DECORATION
-------------------------------------------------
**Dark, with the orange accent, because of where this is read.** The report is a
dark deck with an amber accent, which is what the audience of this talk sees on a
projector. Dark is the default `:root` and there is NO `prefers-color-scheme: light`
branch -- deliberately, because a media query hands the choice to the reading
machine, and the machines that matter here report light: headless Chrome does, and so
does a presenter's laptop nobody switched over. A light theme is still fully
specified and reachable, by an explicit `data-theme="light"` stamp from a host page
that has decided it wants one.

**No JavaScript and no external asset.** Sections collapse with native `<details>`,
so the report behaves the same opened from S3, from a file, or dragged onto a
projector with no network.

**THIS PAGE ASKS FOR NOTHING, AND IT USED TO.** It carried Approve and Reject
controls for every fact research had proposed, because approval happened after the
report was written. The `Approval` stage now pauses the run BEFORE `Present`, so
every decision is settled by the time this renders: an approved fact reads out of
the fact store like any other, and a refused or undecided one is an honest
UNRESOLVED. The controls, the awaiting-approval markers and the awaiting tile are
all gone, and every cell now has exactly one source.

**Status colour never carries meaning alone.** Every verdict and confidence pill
pairs its hue with a glyph and a word, because on a light surface the warning and
serious steps sit below 3:1 by design. The label text wears the ink token, not the
status hue; the hue is on the glyph and the pill's edge.

**UNRESOLVED is deliberately neutral, not red.** A missing fact is an absence of
knowledge, not a finding about the cluster -- the invariant the whole pipeline is
arranged to protect. Painting it as a failure would assert exactly what Stage 3
refuses to assert.

**Proportional figures on the tiles, tabular figures in the tables.** A tile value
is a headline and reads loose with tabular digits; a column of versions must align.
"""

from __future__ import annotations

import html as html_escape
import re

from ..model import (
    ABSENT,
    AdviceFinding,
    AdvicePanel,
    AdviceView,
    ComponentGroup,
    ComponentRow,
    PendingApproval,
    Provenance,
    ReportView,
    StepAdvice,
    StepRow,
    UNSUPPORTED_AT_TARGET_KIND,
    UnadvisedStep,
    UnresolvedOrigin,
    UnresolvedRow,
)

# Status roles, from a palette validated against both surfaces. These four are
# reserved for state and are never reused as a category colour.
_GOOD = "var(--status-good)"
_WARNING = "var(--status-warning)"
_SERIOUS = "var(--status-serious)"
_CRITICAL = "var(--status-critical)"
_NEUTRAL = "var(--text-muted)"

# verdict -> (hue, glyph, label). The glyph and the label are the mitigation for
# the two light-surface steps that fall below 3:1; neither is optional.
_VERDICT_PILL: dict[str, tuple[str, str, str]] = {
    "COMPATIBLE": (_GOOD, "&#10003;", "Compatible"),
    "REQUIRES_COMPONENT_UPGRADE": (_WARNING, "&#8593;", "Upgrade needed"),
    "INCOMPATIBLE": (_CRITICAL, "&#10005;", "Incompatible"),
    # Neutral on purpose. See the module docstring.
    "UNRESOLVED": (_NEUTRAL, "?", "Unresolved"),
}

_CONFIDENCE_PILL: dict[str, tuple[str, str, str]] = {
    "HIGH": (_GOOD, "&#9679;&#9679;&#9679;", "High"),
    "MEDIUM": (_WARNING, "&#9679;&#9679;", "Medium"),
    "LOW": (_SERIOUS, "&#9679;", "Low"),
    "UNRESOLVED": (_NEUTRAL, "?", "None recorded"),
}

_STEP_PILL: dict[str, tuple[str, str, str]] = {
    "CLUSTER": ("var(--accent)", "&#8679;", "Cluster"),
    "REQUIRED": (_WARNING, "&#8593;", "Required"),
    "OPTIONAL": (_NEUTRAL, "&#8594;", "Optional"),
    # NOT AN ARROW, BECAUSE THIS ROW GOES NOWHERE. Every other glyph in this map points at the
    # target: the cluster rises to it, a required move climbs toward it, an optional one steps
    # along it. A component no recorded series of which reaches the target is the one row in
    # the sequence that is not travel, and giving it an arrow would put it in the same visual
    # sentence as the steps that are. Critical hue and a cross, paired with the word, because
    # colour never carries meaning alone on this page.
    "UNSUPPORTED": (_CRITICAL, "&#10005;", "Unsupported"),
}

_GROUP_TITLES: dict[ComponentGroup, tuple[str, str]] = {
    ComponentGroup.MANAGED_ADDON: (
        "EKS-managed add-ons",
        "compatibility published by AWS for this cluster",
    ),
    ComponentGroup.HELM_RELEASE: (
        "CNCF add-ons: Helm-installed",
        "one row per Helm release, identified by chart provenance",
    ),
    ComponentGroup.OTHER_WORKLOAD: (
        "CNCF add-ons: other workloads",
        "manifest-installed and bare workloads",
    ),
}

_ORIGIN_NOTES: dict[UnresolvedOrigin, str] = {
    UnresolvedOrigin.BOTH: "Undecided, so not sequenced",
    UnresolvedOrigin.ASSESSMENT_ONLY: "Undecided",
    UnresolvedOrigin.PLAN_ONLY: "Decided, but could not be sequenced",
}


def _e(value: str | None) -> str:
    """Escape any value that came from a cluster, a model, or a fact store."""
    return html_escape.escape(value or "")


_RAW_GITHUB = re.compile(
    r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$"
)
_API_GITHUB_RELEASES = re.compile(
    r"^https://api\.github\.com/repos/([^/]+)/([^/]+)/releases(?:\?.*)?$"
)
_API_GITHUB_RELEASE_TAG = re.compile(
    r"^https://api\.github\.com/repos/([^/]+)/([^/]+)/releases/tags/([^?]+)$"
)


def _browse_url(url: str | None) -> str:
    """The URL a person opens, for a URL the agent fetched.

    THE FACT KEEPS THE RAW URL; THE PAGE SHOWS THE READABLE ONE. `raw.githubusercontent.com`
    and `api.github.com` are what the fetch tool reads -- bytes with no chrome, which is what
    the quote check verifies against -- and they are a poor thing to hand a reader: raw text
    with no line numbers, or a JSON document. The same content on `github.com` is the page a
    person would look at. Only the href changes, and only for shapes this function recognises;
    anything else is passed through untouched, so a link is never invented.
    """
    if not url:
        return ""
    m = _RAW_GITHUB.match(url)
    if m:
        owner, repo, rev, path = m.groups()
        return f"https://github.com/{owner}/{repo}/blob/{rev}/{path}"
    m = _API_GITHUB_RELEASE_TAG.match(url)
    if m:
        owner, repo, tag = m.groups()
        return f"https://github.com/{owner}/{repo}/releases/tag/{tag}"
    m = _API_GITHUB_RELEASES.match(url)
    if m:
        owner, repo = m.groups()
        return f"https://github.com/{owner}/{repo}/releases"
    return url


def _pill(hue: str, glyph: str, label: str, *, mono: bool = False) -> str:
    """A status pill: hue on the glyph and the edge, ink token on the label."""
    font = "font-family:var(--mono);" if mono else ""
    return (
        f'<span class="pill" style="--hue:{hue};{font}">'
        f'<span class="pill-glyph" aria-hidden="true">{glyph}</span>'
        f"<span>{_e(label)}</span></span>"
    )


def _verdict_pill(verdict: str) -> str:
    hue, glyph, label = _VERDICT_PILL.get(verdict, (_NEUTRAL, "?", verdict))
    return _pill(hue, glyph, label)


def _confidence_pill(confidence: str) -> str:
    if confidence == ABSENT:
        return f'<span class="muted">{_e(ABSENT)}</span>'
    hue, glyph, label = _CONFIDENCE_PILL.get(
        confidence, (_NEUTRAL, "?", confidence)
    )
    return _pill(hue, glyph, label)


def _version(value: str | None, *, emphasis: bool) -> str:
    if not value:
        return f'<span class="muted">{_e(ABSENT)}</span>'
    cls = "ver ver-move" if emphasis else "ver"
    return f'<span class="{cls}">{_e(value)}</span>'


def _label(value: str | None) -> str:
    """An enum name as words: OUTSIDE_STATED_WINDOW -> "outside stated window".

    THE UNDERSCORES WERE BREAKING THE LAYOUT, not just reading badly. A screaming-snake
    token is one unbreakable word, so a narrow column broke it mid-token and rendered
    "OUTSIDE_S TATED_WIN DOW" -- which looks like a rendering fault rather than a value.
    With spaces the browser wraps at word boundaries and the column can stay narrow.

    Lower-cased rather than title-cased: these sit under a version as supporting detail,
    and title case competes with the pills for the eye.
    """
    if not value:
        return ""
    return value.replace("_", " ").lower()


def _clamp(value: str, limit: int = 58) -> str:
    """A citation label short enough not to set the row's height.

    Cut at a WORD boundary, not mid-word, because a label ending "...supported/tes" reads as
    corruption while one ending "...supported" reads as an abbreviation. Anything already
    short is returned untouched, so the common case is unaffected.

    The caller keeps the full text in a `title` attribute; this decides only what is drawn.
    """
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(" ,;:(") + "…"


def _stated(value: str | None) -> str | None:
    """The value, or None when it is absent -- INCLUDING the ABSENT placeholder.

    `ComponentRow` fills an unknown field with the em-dash placeholder rather than with
    None, so `if row.confidence:` is TRUE for a row that knows nothing. Every fallback
    below has to test stated-ness rather than truthiness or it silently never fires --
    which is exactly how one component's confidence column stayed a dash while its own
    proposal sat one cell to the right.
    """
    if value is None:
        return None
    text = value.strip()
    return None if not text or text == ABSENT else text


def _installed_cell(row: ComponentRow | None, pending: PendingApproval | None) -> str:
    """The installed version, plus what the evidence says about it.

    "1.13.2" alone does not tell a reader the installed release sits outside its own
    project's stated window. `installed_version_status` does, and it belongs against
    the version it describes rather than in a prose block further right.

    `row` IS OPTIONAL because the approval page has no inventory row to draw from -- it
    runs before `Decide` and renders proposals alone. Passing None is that case, not a
    missing argument, and it falls through to the proposal's own value.
    """
    cell = _version(
        (_stated(row.installed_version) if row is not None else None)
        or (pending.installed_version if pending else None),
        emphasis=False,
    )
    if pending is not None and pending.installed_version_status:
        cell += f'<div class="sub">{_e(_label(pending.installed_version_status))}</div>'
    return cell


def _confidence_cell(row: ComponentRow | None, pending: PendingApproval | None) -> str:
    """Confidence, and where it came from.

    The stored fact's confidence when there is one. Otherwise the proposal's -- with the
    model's own proposal beside the machine's ceiling when the two differ, because which
    of them capped the result is the most useful thing a reader can know about a rating.
    """
    if row is not None and _stated(row.confidence):
        return _confidence_pill(row.confidence)
    if pending is None:
        return f'<span class="muted">{_e(ABSENT)}</span>'
    cell = _confidence_pill(pending.confidence)
    if (
        pending.proposed_confidence
        and pending.machine_confidence
        and pending.proposed_confidence != pending.machine_confidence
    ):
        cell += (
            f'<div class="sub">model {_e(pending.proposed_confidence)} / '
            f'evidence {_e(pending.machine_confidence)}</div>'
        )
    if pending.evidence_tier:
        cell += f'<div class="sub">{_e(_label(pending.evidence_tier))}</div>'
    return cell


def _recommended_cell(row: ComponentRow | None, pending: PendingApproval | None) -> str:
    """The recommended version: the plan's, or the proposal's when the plan has none.

    `view/build.py`'s `_recommended_version` returns None for an UNRESOLVED verdict, so
    a component with no stored fact could never show a recommendation by that route --
    which is every component research just researched. The proposal fills it, marked as
    proposed rather than presented as an established plan step.
    """
    if row is not None and _stated(row.recommended_version):
        cell = _version(row.recommended_version, emphasis=not row.stays)
        if row.stays:
            cell += '<div class="sub">stay on this version</div>'
        elif row.recommendation_unverified:
            cell += (
                '<div class="sub">newest recorded release &middot; compatibility '
                "unverified</div>"
            )
        return cell
    if pending is None or not pending.recommended_version:
        return f'<span class="muted">{_e(ABSENT)}</span>'
    cell = _version(pending.recommended_version, emphasis=True)
    bits: list[str] = []
    # "KEEP" BESIDE "DOES NOT COVER TARGET" READ AS A CONTRADICTION (kyverno 1.19.1: newest
    # release, supported only up to 1.35). Both halves are true, so they are said as one clause:
    # stay on this version because it is the newest, and it still does not reach the target.
    keep_short = (
        (pending.action or "").upper() == "KEEP" and pending.covers_target_kubernetes is False
    )
    if keep_short:
        bits.append("newest release, stay on it")
    elif pending.action:
        bits.append(_e(_label(pending.action)))
    # Coverage, stated only when the evidence stated it -- see _proposal for why None
    # must not render as False.
    if pending.covers_target_kubernetes is True:
        bits.append("covers target")
    elif pending.covers_target_kubernetes is False:
        bits.append("does NOT cover target")
    if bits:
        cell += f'<div class="sub">{" &middot; ".join(bits)}</div>'
    if pending.upgrade_ordering:
        cell += f'<div class="sub">{_e(_label(pending.upgrade_ordering))}</div>'
    cell += _chart_line(pending)
    return cell


def _chart_line(pending: PendingApproval) -> str:
    """Where the lane will fetch the chart from, when the fact says. Nothing otherwise.

    A managed add-on or a manifest has no chart and gets no line. For a Helm component the
    approver is approving the repository `helm repo add` will be handed; the chart version
    that ships the recommended release is asked of that repository at upgrade time, so it is
    not shown here as if it were already known.
    """
    if not pending.chart_repository:
        # A HELM PROPOSAL WITH NO REPOSITORY IS SHOWN, NOT BURIED. The judge stage adds a
        # `chart repository:` not-found entry for exactly that case; it is lifted out of the
        # collapsed caveats into this column because it decides whether the lane can run.
        # The prefix is `research.report.CHART_MISSING_PREFIX`, spelled here because a
        # renderer does not import a stage-side module.
        if any(g.strip().lower().startswith("chart repository:") for g in pending.not_found):
            return (
                '<div class="sub nocover">no chart repository &middot; the upgrade lane '
                "cannot install this</div>"
            )
        return ""
    return f'<div class="sub">chart from {_e(pending.chart_repository)}</div>'


def _sources_cell(
    row: ComponentRow | None, pending: PendingApproval | None = None
) -> str:
    """The sources column, from the stored fact -- or from the proposal awaiting approval.

    A component with no stored fact has no `row.sources`, which is why this column read
    as a dash for exactly the rows a reader most needs sources for. The pending
    assessment's `cited_sources` is the same kind of thing -- documents, with what each
    was used for -- so it fills the same column rather than being buried elsewhere.
    """
    sources = row.sources if row is not None else ()
    if not sources and pending is not None and pending.cited_sources:
        # A real list, not <br>-joined text: several citation titles run to a full
        # sentence each, and separated only by a line break they read as one paragraph.
        #
        # AND THE LABEL IS CLAMPED, because the model writes `used_for` as prose and it is
        # routinely a whole sentence -- "compatibility matrix (current, upcoming, and old
        # releases mapped to supported/tested Kubernetes and OpenShift versions, and general
        # support policy)". Rendered in full it wrapped to eight lines and made one cell taller
        # than the rest of the row put together, which is what the column being unreadable
        # actually was. The full text stays reachable as the link's `title`, so nothing is
        # lost -- it is just no longer allowed to set the row's height.
        items = "".join(
            f'<li><a href="{_e(_browse_url(c.url))}" rel="noreferrer noopener" '
            f'title="{_e(c.used_for)}">{_e(_clamp(c.used_for))}</a></li>'
            for c in pending.cited_sources
        )
        return f'<ul class="cited">{items}</ul>'
    if not sources:
        return f'<span class="muted">{_e(ABSENT)}</span>'
    parts: list[str] = []
    for source in sources:
        star = (
            ' <span class="star" title="authoritative source" '
            'aria-label="authoritative source">&#9733;</span>'
            if source.official
            else ""
        )
        if source.url:
            parts.append(
                f'<a href="{_e(_browse_url(source.url))}" rel="noreferrer noopener">'
                f"{_e(source.title)}</a>{star}"
            )
        else:
            parts.append(f"<strong>{_e(source.title)}</strong>{star}")
    cell = "<br>".join(parts)
    synopsis = next((s.synopsis for s in sources if s.synopsis), "")
    if synopsis:
        cell += f'<div class="synopsis">{_e(synopsis)}</div>'
    return cell


def _where(component: str, namespace: str | None, workload_ref: str) -> str:
    """The sub-line under a component name: where it actually lives.

    Suppressed entirely when the display name IS the workload reference -- a
    component with no resolved identity would otherwise print its own name twice,
    which reads as a rendering fault rather than as missing information.
    """
    if component == workload_ref:
        return f'<div class="sub">{_e(namespace)}</div>' if namespace else ""
    if namespace:
        return f'<div class="sub">{_e(namespace)} &middot; {_e(workload_ref)}</div>'
    return f'<div class="sub">{_e(workload_ref)}</div>'


def _component_row(row: ComponentRow) -> str:
    """One component, entirely from the fact store. No decision, no proposal, no control.

    THE DECISION COLUMN IS GONE, and its removal is a consequence of the pipeline changing
    shape rather than a simplification of the page. Approval used to happen AFTER this report
    -- research proposed, the report displayed the proposal with an approve control, and a
    person acted on it later -- so the report had to carry the pending fact and its controls.

    The `Approval` stage now pauses the run BEFORE `Present`, so by the time this renders
    every decision has been made: an approved fact is in the store and reads out of
    `ComponentRow` like any other, and a rejected or undecided one is an honest UNRESOLVED.
    There is nothing left for a reader to act on here, so a control would be furniture.

    That also restores a single source of truth. The `PendingApproval` fallbacks these cells
    used to take existed only because the fact was not stored yet when the report rendered;
    with approval upstream they are dead on every path, and a column fed from two places is
    a column that can disagree with itself.
    """
    where = _where(row.component, row.namespace, row.workload_ref)
    return (
        '<tr class="arow">'
        f'<td><span class="name">{_e(row.component)}</span>{where}</td>'
        f"<td>{_installed_cell(row, None)}</td>"
        f"<td>{_verdict_pill(row.verdict)}</td>"
        f"<td>{_confidence_cell(row, None)}</td>"
        f"<td>{_recommended_cell(row, None)}</td>"
        f'<td class="src">{_sources_cell(row, None)}</td>'
        "</tr>"
    )


def _needs_action(rows: list[ComponentRow]) -> bool:
    """Whether a group holds anything a reader has to do something about.

    Used only to decide which groups start expanded, so it is a display decision and
    not a computed value -- it reads verdicts the assessment already formed and
    derives no number. The rule earns its place because the alternative shipped a
    real defect in the first screenshot of this page: groups opened by POSITION, so
    the managed add-ons (all four compatible, nothing to do) were expanded while the
    Helm group holding the run's single upgrade-needed component was collapsed. On a
    projector that reads as a clean bill of health.
    """
    return any(r.verdict != "COMPATIBLE" for r in rows)


def _group_block(view: ReportView, group: ComponentGroup) -> str:
    rows = [r for r in view.components if r.group is group]
    if not rows:
        return ""
    title, subtitle = _GROUP_TITLES[group]
    body = "".join(_component_row(r) for r in rows)
    # NO AWAITING-APPROVAL PILL. It used to sit on the bar so a decision inside a COLLAPSED
    # group was still visible without opening it. Nothing awaits a decision at this point in
    # the pipeline any more -- the `Approval` stage settled everything before `Present` ran --
    # so the pill would always read zero and the only honest number left is the row count.
    return (
        '<details class="card">'
        '<summary><span class="sum-title">'
        f"{_e(title)}</span><span class=\"sum-sub\">{_e(subtitle)}</span>"
        f'<span class="count">{len(rows)}</span></summary>'
        '<div class="scroll"><table class="cols6">'
        "<thead><tr><th>Component</th><th>Installed</th><th>Status</th>"
        "<th>Confidence</th><th>Recommended</th>"
        "<th>Source and what the evidence says</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div></details>"
    )


def _step_row(step: StepRow) -> str:
    hue, glyph, label = _STEP_PILL.get(step.label, (_NEUTRAL, "&#8594;", step.label))
    reversible = (
        _pill(_SERIOUS, "&#9888;", "One way")
        if step.one_way
        else (
            f'<span class="muted">{_e(step.reversibility or ABSENT)}</span>'
        )
    )
    unordered = (
        '<div class="sub">order not forced against '
        + ", ".join(f"#{o}" for o in step.unordered_with)
        + "</div>"
        if step.unordered_with
        else ""
    )
    return (
        "<tr>"
        f'<td class="num">{step.order}</td>'
        f"<td>{_pill(hue, glyph, label)}</td>"
        f"<td>{_e(step.description)}{unordered}</td>"
        f"<td>{reversible}</td>"
        "</tr>"
    )


def _sequence_block(
    view: ReportView,
    advice: AdviceView | None = None,
    forms: dict[int, dict[str, dict]] | None = None,
    done: dict | None = None,
    withheld: dict[int, str] | None = None,
) -> str:
    """The ordered plan. Two shapes, and which one appears is decided by `advice`.

    WITHOUT ADVICE it is the read-only card it has always been: four columns, one row per
    step, nothing to act on. That is the shape every existing caller and test sees, and it
    is unchanged.

    WITH ADVICE this card becomes the page's whole point, so it changes shape rather than
    growing a column. It takes the six columns the fact-approval table already uses --
    component, installed, recommended, then the controls -- because a person who has read
    the component table two cards up recognises that layout instead of re-learning it, and
    because the one long prose sentence per row ("Move cert-manager: 1.13.x -> 1.19.x
    (install 1.19.6) at Kubernetes 1.35") is four facts welded into a string a reader has
    to parse. Broken apart, each fact sits in the column that owns it.

    Kind and Reversibility leave the table in that shape. Kind becomes a word under the
    component name; Reversibility read UNKNOWN on every add-on move in every run this has
    seen, and a column that never varies teaches a reader to stop looking at it. The
    classification still exists on the step.
    """
    if advice is None:
        if view.steps:
            body = "".join(_step_row(s) for s in view.steps)
        else:
            body = (
                '<tr><td colspan="4" class="muted">No steps could be sequenced.</td></tr>'
            )
        return (
            # CLOSED, like every other card: the report opens as a summary now that it
            # carries no controls, and four wide tables at once bury the tiles a reader
            # starts from.
            '<details class="card">'
            '<summary><span class="sum-title">Upgrade sequence</span>'
            '<span class="sum-sub">cluster hops and component moves, in order</span>'
            f'<span class="count">{view.totals.steps}</span></summary>'
            '<div class="scroll"><table class="cols4 steps">'
            "<thead><tr><th>#</th><th>Kind</th><th>Action</th>"
            "<th>Reversibility</th></tr></thead>"
            f"<tbody>{body}</tbody></table></div></details>"
        )

    withheld = withheld or {}
    # NO FORMS AT ALL MEANS NO DECISION COLUMN. That is the readiness report -- a deployment with
    # the upgrade stage off -- and a column of "No controls rendered" on every row told the
    # reader six times what the notice at the top already said once. The approval page passes a
    # dict (possibly empty after filtering) and keeps the column, with its buttons or its reasons.
    decisions = forms is not None
    columns = columns_for(decisions)
    rows = "".join(
        _decision_row(step, advice, forms or {}, withheld, decisions=decisions)
        for step in view.steps
    ) or f'<tr><td colspan="{columns}" class="muted">No steps could be sequenced.</td></tr>'
    # ONLY WHAT IS ACTUALLY OFFERED IS COUNTED AS AWAITING. A row whose controls were withheld
    # asks nothing of the reader, and counting it would tell them seven decisions are pending
    # when four are.
    waiting = sum(
        1 for s in view.steps if s.kind != "CLUSTER_UPGRADE" and s.order not in withheld
    )
    sub = (
        f"in order &middot; {waiting} awaiting your approval"
        if decisions
        else "in order &middot; recommendations, with their briefs"
    )
    return (
        # OPEN, unlike every other card on this page, because it is the only one that asks
        # anything of the reader. The rest of the report is context they may want; this is
        # the decision they were sent here to make.
        '<details class="card" id="sequence" open>'
        '<summary><span class="sum-title">Upgrade sequence</span>'
        f'<span class="sum-sub">{sub}</span>'
        f'<span class="count">{view.totals.steps}</span></summary>'
        f"{_pause_note(done)}{_not_reached_note(view)}"
        '<div class="scroll"><table class="seq">'
        "<thead><tr><th>#</th><th>Component</th><th>Installed</th><th>Recommended</th>"
        "<th>Details</th>" + ("<th>Decision</th>" if decisions else "") + "</tr></thead>"
        f"<tbody>{rows}</tbody></table></div>{_done_block(done)}</details>"
    )


def _blocker_names(view: ReportView) -> list[str]:
    """The components that keep the cluster from reaching the target, in report order.

    Read straight off `view.unresolved` -- the same rows the Unresolved table shows -- so the
    header never names a component the table does not, and no count is computed here.
    """
    names: list[str] = []
    for row in view.unresolved:
        if row.component not in names:
            names.append(row.component)
    return names


def _readiness_pill(view: ReportView) -> str:
    """The header verdict, phrased as readiness for the target, not as plan feasibility.

    `feasible: false` means some component has no recorded version that supports the target
    Kubernetes version. It does not block the other steps, and the cluster upgrade itself is
    always the operator's call, so the pill is a warning that names the unresolved components
    rather than a "not feasible" or "blocked" verdict on the whole plan.
    """
    target = _e(view.target_kubernetes_version)
    if view.feasible:
        return _pill(_GOOD, "&#10003;", f"Ready for Kubernetes {target}")
    names = _blocker_names(view)
    if not names:
        return _pill(_WARNING, "&#9888;", f"Kubernetes {target}: unresolved components")
    return _pill(
        _WARNING,
        "&#9888;",
        f"Kubernetes {target}: {len(names)} unresolved ({_e(', '.join(names))})",
    )


def _not_reached_note(view: ReportView) -> str:
    """`feasible: false`, in words, before the rows that could otherwise hide it.

    THE HEADER ALREADY CARRIES A "NOT FEASIBLE" PILL AND THAT WAS ENOUGH UNTIL THIS TABLE GREW
    A ROW FOR THE COMPONENT CAUSING IT. A pill is a glance; a table of Approve buttons is a
    task, and the reasonable inference from working down a task list and answering every row is
    that answering them all completes the thing. It does not: approving an
    unsupported-at-target row records a per-cluster acceptance and leaves `feasible` exactly as
    false as it was. So the page says which components will not make the jump, and says the
    consequence of approving them, in the same block as the controls rather than one pill away
    at the top of the document.

    EMITTED ONLY WHEN THERE IS SUCH A ROW, not whenever the plan is infeasible. The misreading
    this prevents is created by these rows, so the correction appears with them -- which also
    means the BODY of an approval page for a plan that carries none is byte-identical to what it
    was. Its stylesheet is not: `_ADVICE_CSS` is emitted whole for any page carrying advice, so
    the two rules this note needs travel with every upgrade approval page whether it draws one
    or not. That is the same trade `_CSS` refuses and `_ADVICE_CSS` was extracted to make -- the
    read-only report and the FACT approval page, which share `_CSS` and `_CONTROL_CSS`, are
    untouched, and those are the artifacts whose bytes something else depends on.

    The components are NAMED rather than counted. A count would be a fifth displayed number
    computed outside `ReportTotals`, which is the one thing this package forbids a renderer; the
    names are read straight off the rows the reader is looking at.
    """
    rows = [s for s in view.steps if s.unsupported_at_target]
    if not rows:
        return ""
    names = ", ".join(_step_parts(s)[0] for s in rows)
    # THE MOVE CLAUSE IS CONDITIONAL, because it is not true of every one of these rows. A
    # component already running the newest release anybody recorded has nothing to move to, and
    # telling its reader that approving takes it further would be exactly the invented progress
    # this note exists to deny. Emitted when at least one of the rows really does carry a move --
    # which is also why the sentence never says which: the rows themselves say it, per component,
    # and a second list here would be a fifth displayed value computed by a renderer.
    further = (
        "Where a row carries a newer release, approving it upgrades the component that far "
        "<strong>and</strong> records that <strong>this cluster</strong> accepts running it "
        "unsupported. A row already on the newest recorded release has nothing to upgrade and "
        "carries no button"
        if any(_unsupported_move(s) is not None for s in rows)
        else "These components are already on the newest recorded release, so there is nothing "
        "to upgrade and the rows carry no button"
    )
    return (
        '<p class="notreached">This plan does <strong>not</strong> reach Kubernetes '
        f"{_e(view.target_kubernetes_version)} for every component. "
        f"<strong>{_e(names)}</strong> &mdash; no recorded version series supports "
        f"{_e(view.target_kubernetes_version)}. {further}. Whether to upgrade the cluster "
        "anyway is your decision.</p>"
    )


def _pause_note(done: dict | None) -> str:
    """What the reader must know BEFORE deciding anything, said before the table.

    THE SAME THING THE FACT APPROVAL PAGE SAYS AT THE TOP -- *"the run is paused here and will
    not reach its report until you finish"* -- and it was missing here. The equivalent sentence
    existed only in the Done bar, which sits BELOW eight rows of decisions: a reader who worked
    down the table pressing Approve had no way to know, until they got there, that none of it
    continues until Done is pressed. Somebody could decide everything, close the tab, and leave
    the execution to die at its timeout believing they had finished.

    Stated as plainly as possible and only when there is a Done control to press. The read-only
    report is not paused on anything and must not claim to be.
    """
    if not done:
        return ""
    return (
        '<p class="pausenote">The run is <strong>paused here</strong> and changes nothing '
        "on the cluster until you finish. Decide each row below, then press "
        "<strong>Done</strong> to hand the run back &mdash; without that, nothing carries "
        "on and the run eventually times out. Anything you leave undecided is offered "
        "again on the next run, and is never read as a refusal.</p>"
    )


def _done_block(done: dict | None) -> str:
    """The control that hands the execution back, and WITHOUT IT NOTHING EVER RESUMES.

    Every Approve and Reject writes a durable `StepDecision` to S3 the moment it lands, so a
    page with no Done still records answers perfectly -- and the run still dies at its timeout,
    because `SendTaskSuccess` is only reached by the watcher redeeming the DONE grant. The stage
    minted that grant from the first version; nothing ever built it a form. So the page offered
    sixteen working buttons and no way to finish, and the failure looked like a timeout rather
    than a missing control.

    SEPARATE FROM THE PER-STEP FORMS, and after the table rather than in it: it is not a
    decision about any step. It says "I have finished deciding", which is why the watcher treats
    it as the only signal that may resume the run.

    Absent when no form was supplied -- the read-only report has no controls at all and must
    not grow one.
    """
    if not done:
        return ""
    return (
        '<div class="donebar">'
        "<div><b>Finished deciding?</b> Every answer above is already recorded. This hands the "
        "run back so it can carry on &mdash; anything you left undecided is offered again next "
        "run, and is never read as a refusal.</div>"
        f'{_decision_form(done, "Done", "done")}'
        "</div>"
    )


# The words a reader wants instead of the plan's own step kinds.
#
# "RECOMMENDED MOVE", NOT "OPTIONAL UPGRADE", AND THE DIFFERENCE IS THE WHOLE POINT OF THE
# CELL. What is optional about a `COMPONENT_UPGRADE_RECOMMENDED` step is that the CLUSTER
# UPGRADE does not depend on it -- decline it and the cluster still reaches the target. What
# is not optional is the answer: the step is in the plan, it sits on this page beside its own
# Approve and Reject controls, and it is waiting on the reader either way. "Optional" said
# the second thing while meaning the first, and a reader who reads "optional upgrade" beside
# a decision they are being asked for reasonably concludes the row can be ignored. The
# distinction from `required move` survives in the word "recommended".
#
# "NO SUPPORTED VERSION AT TARGET" IS WORDED AS A DEAD END ON PURPOSE, and every phrasing that
# reads as a move was rejected. Not "must be replaced", which is an instruction this page has no
# evidence for -- nobody researched a replacement. Not "blocked", which invites the reading that
# approving the row unblocks something. Not "unsupported move", because there is no move: the
# component stays exactly where it is and the cluster goes on without it. The cell states the
# fact -- no version supports the target -- and the row's other cells state what approving it
# does and does not mean.
_KIND_WORDS: dict[str, str] = {
    "COMPONENT_MOVE": "required move",
    "COMPONENT_UPGRADE_RECOMMENDED": "recommended move",
    "CLUSTER_UPGRADE": "control-plane hop",
    UNSUPPORTED_AT_TARGET_KIND: "no supported version at target",
}

# `StepRow.description` is prose the plan composed, and the six-column table needs its
# parts. Parsed rather than re-derived: `view/build.py` owns the sentence, and a renderer
# recomputing "which versions is this step between" from the plan is exactly what the
# import guard exists to prevent. If a pattern does not match, the cells fall back to the
# whole sentence rather than to a guess -- an unparsed row is ugly and honest.
_MOVE = re.compile(
    r"Move (?P<component>[^:]+): (?P<installed>\S+) \u2192 \S+ "
    r"\(install (?P<recommended>[^)]+)\) at Kubernetes (?P<at>[\d.]+)"
)
_OPTIONAL = re.compile(
    r"upgrade (?P<component>\S+) (?P<installed>\S+) \u2192 (?P<recommended>\S+) "
    r"at Kubernetes (?P<at>[\d.]+)"
)
# The unsupported-at-target sentences -- TWO of them, because the row has two shapes and one
# kind. Both are matched BEFORE the general patterns because their prose is about an upgrade the
# component cannot complete, and a pattern that searches for the bare word "upgrade" anywhere in
# a description is one wording change away from claiming it: first match wins, so the specific
# patterns must be offered before the general ones.
#
# THE MOVE SHAPE. `view/build.py` mints it when the fact records a release strictly newer than
# what is installed, so the row IS a move -- installed series to newest recorded release -- and
# its `recommended` is a version an operator will really install. What it is NOT is a version
# that reaches the target, which is why the pattern requires the disqualification to be part of
# the sentence it parses: a wording change that dropped "does NOT cover Kubernetes N either"
# stops matching here and falls back to the whole description rather than quietly rendering the
# release as though it resolved the upgrade.
_UNSUPPORTED_MOVE = re.compile(
    r"Unsupported at target: (?P<component>.+?) has no recorded version series "
    r"supporting Kubernetes (?P<at>[\d.]+)\. Move (?P<installed>\S+) → "
    r"(?P<recommended>[^,\s]+), the newest recorded release, which does NOT cover "
    r"Kubernetes [\d.]+ either\. Installed (?P<running>\S+)\."
)
# THE DEAD-END SHAPE, whose `recommended` is a recorded release that does NOT reach the target
# and is NOT a version to install either -- there is nothing to move to, because the fact
# records nothing newer than what is already running.
#
# THE RELEASE CLAUSE IS AN ALTERNATION, NOT AN OPTIONAL GROUP, so "no release is recorded" is a
# recognised sentence rather than a parse failure. A fact can legitimately record a
# compatibility window and no release inside it, and a component whose row fell back to the
# whole description in that case would mint a `StepGrant` whose component field is a paragraph.
_UNSUPPORTED = re.compile(
    r"Unsupported at target: (?P<component>.+?) has no recorded version series "
    r"supporting Kubernetes (?P<at>[\d.]+)\. Installed (?P<installed>[^;]+); "
    r"(?:newest recorded release (?P<recommended>\S+) does NOT cover target"
    r"|no release is recorded)\."
)


def _step_parts(step: StepRow) -> tuple[str, str, str, str]:
    """`(component, installed, recommended, at_kubernetes)` for one step.

    The cluster hop has no component version to move, so it names the cluster and its two
    Kubernetes versions -- which is what a reader looking down the Installed column for
    "what is on my cluster now" expects to find there.

    AN UNSUPPORTED-AT-TARGET ROW'S `recommended` NEVER REACHES THE TARGET, whichever of its two
    sentences matched, and the caller is required to mark it as not covering the target either
    way. On the move shape it IS a version an operator installs -- the newest release the fact
    records, which the component really can move to -- and on the dead-end shape it is a
    recorded release with nothing to move to. `_unsupported_move` is how the caller tells them
    apart; this function deliberately does not, because its four values are the same four in both
    cases and a fifth return value would have to be threaded through every other kind.
    Returned EMPTY when the fact records no release, rather than as the `ABSENT` placeholder the
    fallback below uses: an empty string is a value the caller can test, whereas an em-dash is
    already a rendering decision, and this row's caller needs to say something more specific
    about the absence than a dash.
    """
    for pattern in (_UNSUPPORTED_MOVE, _UNSUPPORTED, _MOVE, _OPTIONAL):
        found = pattern.search(step.description)
        if found is not None:
            return (
                found["component"],
                found["installed"],
                found["recommended"] or "",
                found["at"],
            )
    if step.kind == "CLUSTER_UPGRADE":
        hop = re.search(r"([\d.]+) \u2192 ([\d.]+)", step.description)
        if hop is not None:
            return "the cluster", hop[1], hop[2], ""
    return step.description, ABSENT, ABSENT, ""


def _decision_row(
    step: StepRow,
    advice: AdviceView,
    forms: dict[int, dict[str, dict]],
    withheld: dict[int, str] | None = None,
    *,
    decisions: bool = True,
) -> str:
    """One step, plus the full-width row holding its brief.

    THE BRIEF ROW IS `tr.cmdrow`'S IDEA, one document further on: a row that spans every
    column, drops out of the grid, and is revealed by `:has()` on the row above -- with `+`
    keeping the match adjacent so one step's control can never open another's. The
    `@supports` gate below is what makes that safe: where `:has()` does not work the row is
    simply never hidden, so the toggle degrades to a no-op beside content already on
    screen rather than to a control that opens nothing.

    AN UNSUPPORTED-AT-TARGET ROW IS DRAWN BY ITS OWN FUNCTION AND RETURNS BEFORE ANY OF THIS.
    Not to keep this function short -- to make it structurally impossible for the new row to
    change the bytes of the rows that were already here. Every cell below is shared by three
    kinds of real plan step and by the fact-approval page through `_recommended_cell`; adding a
    fourth kind as four more conditionals inside them is how a page that renders correctly for
    the new case starts rendering differently for the old ones.
    """
    if step.unsupported_at_target:
        return _unsupported_at_target_row(
            step,
            advice,
            forms.get(step.order),
            decisions=decisions,
            withheld=(withheld or {}).get(step.order),
        )

    component, installed, recommended, at_kubernetes = _step_parts(step)
    kind = _KIND_WORDS.get(step.kind, _label(step.kind))
    unordered = (
        " &middot; order not forced against "
        + ", ".join(f"#{o}" for o in step.unordered_with)
        if step.unordered_with
        else ""
    )

    brief = advice.for_step(step.order)
    absent = advice.absence_for(step.order)
    if brief is not None:
        details = _DETAILS_TOGGLE
    elif absent is not None:
        details = _no_brief(absent.reason)
    elif step.aws_managed:
        # NOT "NO BRIEF", BECAUSE NOTHING FAILED. An AWS-managed add-on is excluded from the
        # brief on purpose (`brief/document.py::excluded_reason`): AWS publishes the version,
        # has already qualified it against this Kubernetes minor, and there is no chart to
        # reconfigure, so there are no release notes anybody was meant to read. "No brief /
        # nothing read" reports that decision as a research failure, which invites a reader
        # to hold the step back waiting for something that is never coming. The step still
        # carries its Approve and Reject controls -- see `_decision_cell`.
        # SHAPED LIKE THE `Details` BUTTON IT SITS BESIDE, because the first version was not: a
        # full sentence in a column sized for a small chip wrapped onto three ragged lines and
        # pulled the row out of alignment with every briefed row above it. The column sets the
        # rhythm and the cell has to keep it.
        #
        # THE REASON MOVES INTO `title`, not out of the page. It is the same sentence one hover
        # away, and a reader who already knows what an EKS-managed add-on is does not have to
        # read it in every one of four rows.
        details = (
            '<span class="nodet nabadge" title="AWS publishes and qualifies this add-on'
            " version against this Kubernetes minor itself, and there is no Helm chart to"
            ' reconfigure, so no release notes were owed and none were read.">'
            "Brief: N/A</span>"
        )
    else:
        # A step nothing briefed and nothing explained. The cluster hop is the honest case:
        # it moves no component version, so there is no installed-to-recommended jump to
        # read release notes across. Any other step reaching here is a move whose brief was
        # never produced (the brief stage failed as a whole), and must not be described as
        # moving nothing.
        details = _no_brief(
            "moves no component version"
            if step.kind == "CLUSTER_UPGRADE"
            else "no brief was produced for this move"
        )

    row = (
        f'<tr class="step" id="s{step.order}">'
        f'<td class="num">{step.order}</td>'
        f'<td><span class="name">{_e(component)}</span>'
        f'<div class="sub">{_e(kind)}{unordered}</div></td>'
        f"<td>{_version(installed, emphasis=False)}</td>"
        f"<td>{_version(recommended, emphasis=step.kind != 'CLUSTER_UPGRADE')}"
        + (
            f'<div class="sub">at kube {_e(at_kubernetes)}</div>'
            if at_kubernetes
            else ""
        )
        + "</td>"
        f"<td>{details}</td>"
        + (
            '<td class="dec">'
            + _decision_cell(
                step, forms.get(step.order), withheld=(withheld or {}).get(step.order)
            )
            + "</td>"
            if decisions
            else ""
        )
        + "</tr>"
    )
    if brief is None:
        return row
    return row + _brief_row(brief, columns=columns_for(decisions))


# THE THREE PIECES OF THE DETAILS COLUMN, NAMED ONCE BECAUSE TWO ROW BUILDERS DRAW THEM.
#
# `_decision_row` draws the three real plan kinds and `_unsupported_at_target_row` draws the
# fourth, and both now have to offer a brief: a component that cannot reach the target still gets
# briefed on the move it CAN make, so the toggle, the absence sentence and the full-width brief
# row all appear on both. Two hand-maintained copies of this markup would drift the moment either
# row changed -- and the failure would be a brief that renders in a row that no longer opens it,
# which is invisible in a diff and obvious only to a reader who clicks.
_DETAILS_TOGGLE = '<details class="det"><summary class="detbtn">Details</summary></details>'


def _no_brief(reason: str) -> str:
    """The Details cell for a row nothing briefed, with the reason under it.

    CLAMPED, because the column is sized for a small chip: a full sentence in it wraps onto three
    ragged lines and pulls the row out of alignment with every briefed row above it. The reason is
    shortened at a word boundary rather than dropped -- "no brief" with no account of why is the
    silence this whole surface exists to replace.
    """
    return (
        '<span class="muted nodet">No brief</span>'
        f'<div class="sub">{_e(_clamp(reason, 64))}</div>'
    )


def columns_for(decisions: bool) -> int:
    """How many columns the sequence table has: six with a Decision column, five without."""
    return 6 if decisions else 5


def _brief_row(brief: StepAdvice, *, columns: int = 6) -> str:
    """The full-width row holding one step's brief, revealed by the toggle on the row above.

    `tr.cmdrow`'S IDEA one document further on: a row that spans every column, drops out of the
    grid, and is revealed by `:has()` on the row above -- with `+` keeping the match adjacent, so
    one step's control can never open another's. That adjacency is why this is returned by the
    row builder and appended immediately, rather than collected and emitted after the table.
    """
    return (
        f'<tr class="briefrow"><td colspan="{columns}">'
        f"{_advice_fields(brief)}"
        "</td></tr>"
    )


def _unsupported_move(step: StepRow) -> re.Match[str] | None:
    """The match when this row's own sentence describes a move, or None when it describes a stay.

    THE ONE QUESTION THAT SPLITS THE ROW INTO ITS TWO SHAPES, asked of the sentence
    `view/build.py` composed rather than of a second field, for the reason that module states
    about the whole description: the rules layer owns which components move and how far, and a
    renderer deciding it for itself is the class of defect the presentation boundary exists to
    remove. Returns the match rather than a bool so a caller that needs the parts does not parse
    the same sentence twice.
    """
    return _UNSUPPORTED_MOVE.search(step.description)


def _unsupported_at_target_row(
    step: StepRow,
    advice: AdviceView,
    form: dict[str, dict] | None,
    *,
    decisions: bool = True,
    withheld: str | None = None,
) -> str:
    """A component no recorded version of which reaches the target, as a decidable row.

    THE ONE HARD REQUIREMENT ON THIS FUNCTION IS THAT IT MUST NOT READ AS PROGRESS TOWARD THE
    TARGET. It sits in a table of steps that carry a cluster to its target, four columns of which
    are shaped to say "this version becomes that version at this Kubernetes minor" -- so left to
    the shared cells, a recorded release in the Recommended column beside an Approve button would
    read as "approve this and keda is fine and you are done". That is false however far the
    component moves: 2.20.2 does not cover 1.36, and the plan still does not reach the target.

    IT NOW CARRIES A REAL MOVE, AND BOTH HALVES HAVE TO SURVIVE IN THE SAME ROW. When the fact
    records a release strictly newer than what is installed, approving this row means *upgrade
    keda as far as it goes AND accept that 1.36 stays unsupported* -- so the Recommended cell
    paints a version an operator really installs, in the same ink as any other move target, and
    the disqualification sits directly under it in the critical ink. A muted note under a version
    number is exactly what a reader skims past, and this is the note that must not be skipped.

    THE DEAD-END SHAPE IS UNCHANGED, byte for byte. When the fact records nothing newer than what
    is running there is no move to offer, and inventing `2.20.x -> 2.20.2` for a cluster already
    on 2.20.2 would be a fabricated step beside a live Approve button.

    Wording follows the register the fact-approval page already uses for the adjacent idea, "does
    NOT cover target", because a reader who has seen that phrase on one page should not have to
    learn a second vocabulary for the same fact on this one.
    """
    component, installed, newest, at_kubernetes = _step_parts(step)
    brief = advice.for_step(step.order)
    move = _unsupported_move(step)
    moving = move is not None
    if move is not None:
        # EMPHASISED, UNLIKE THE DEAD-END SHAPE. `_version(..., emphasis=True)` paints the version
        # a reader is being told to install, and here that is exactly what it is: the component
        # can reach this release and should. What it cannot do is reach the target, which is what
        # the note under it says -- in the critical ink, because the whole risk of this shape is a
        # reader who sees a normal-looking move target and stops reading.
        recommended = _version(newest, emphasis=True) + (
            '<div class="sub nocover">newest recorded release &middot; does '
            f"NOT cover Kubernetes {_e(at_kubernetes)} either</div>"
        )
        # THE SERIES IS IN THE COLUMN AND THE EXACT RELEASE UNDER IT, which is how every other
        # move row reads (`1.13.x` in the column, because that is what the plan moves FROM). The
        # exact installed release stays visible because it is the version running unsupported
        # today, and "which release am I actually on" is the first thing a reader checks.
        installed_cell = _version(installed, emphasis=False) + (
            f'<div class="sub">running {_e(move["running"])}</div>'
        )
    else:
        recommended = (
            (
                _version(newest, emphasis=False)
                + '<div class="sub nocover">newest recorded release &middot; does '
                f"NOT cover Kubernetes {_e(at_kubernetes)}</div>"
            )
            if newest
            else (
                f'<span class="muted">{_e(ABSENT)}</span>'
                '<div class="sub nocover">no release is recorded, and none would '
                "cover the target</div>"
            )
        )
        installed_cell = _version(installed, emphasis=False) + (
            '<div class="sub">stays where it is</div>'
        )
    return (
        f'<tr class="step" id="s{step.order}">'
        f'<td class="num">{step.order}</td>'
        f'<td><span class="name">{_e(component)}</span>'
        f'<div class="sub">{_e(_KIND_WORDS[UNSUPPORTED_AT_TARGET_KIND])}</div>'
        f'<div class="sub nocover">No recorded version series supports Kubernetes '
        f"{_e(at_kubernetes)}.</div></td>"
        f"<td>{installed_cell}</td>"
        f"<td>{recommended}</td>"
        f"<td>{_unsupported_details(brief, advice.absence_for(step.order), moving)}</td>"
        + (('<td class="dec">' + _decision_cell(
            step,
            form,
            # BOTH HALVES IN ONE SENTENCE, IN THE ORDER THEY HAPPEN. A reader about to press
            # Approve is told what it does first -- the component moves as far as its releases go
            # -- and then what it costs, which is the acceptance. One em-dash only: the last
            # clause is the one that must not read as an afterthought, and three of them in a row
            # turn the sentence into a list nobody finishes.
            sub=(
                (
                    f"takes {_e(component)} to {_e(newest)}, as far as its recorded releases "
                    "go, and "
                    if moving
                    else ""
                )
                + f"accepts running {_e(component)} unsupported on this cluster "
                "&mdash; it does not make the upgrade reach "
                f"{_e(at_kubernetes)}"
            ),
            withheld=withheld,
        )
        + "</td>") if decisions else "")
        + "</tr>"
        # THE BRIEF'S OWN ROW, IMMEDIATELY AFTER THIS ONE, which is what `:has() +` relies on to
        # reveal it -- exactly as a plan move's does. Anything between them and the toggle stops
        # opening it.
        + (_brief_row(brief, columns=columns_for(decisions)) if brief is not None else "")
    )


def _unsupported_details(
    brief: StepAdvice | None, absent: UnadvisedStep | None, moving: bool
) -> str:
    """The Details cell for an unsupported-at-target row: the brief, or the reason there is none.

    A BRIEF IS OFFERED HERE ON THE SAME TERMS AS ANY MOVE, which it was not before. The brief
    stage walked `plan.steps` only, so a component in `unsequenceable` got no brief at all and
    this cell always said `Brief: N/A`. It now gets one for the move it CAN make -- the release
    notes between the installed release and the newest recorded one are exactly what an operator
    keeping the component needs -- so the toggle appears here whenever the brief exists.

    THREE ABSENCES AND THEY ARE NOT THE SAME ABSENCE:

        an `unadvised` entry   the brief stage tried and could not. Its own reason.
        a move with no entry   the brief stage never ran, or its document could not be read. The
                               advisory stage may not fail a run, so this is a sentence and the
                               row keeps its controls.
        no move at all         there is nothing to read release notes ACROSS, which is a decision
                               rather than a gap, so it wears the badge shape.

    NEVER THE AWS-MANAGED BADGE'S REASON, WHICH WOULD BE FALSE ON EVERY ONE OF THESE ROWS. That
    badge says AWS has already qualified this version against this Kubernetes minor, so no release
    notes were owed. Nothing has qualified anything here -- that is the entire point of the row --
    and telling a reader otherwise would say the component is fine.
    """
    if brief is not None:
        return _DETAILS_TOGGLE
    if absent is not None:
        return _no_brief(absent.reason)
    if moving:
        return _no_brief("nothing was read about this move")
    # The badge shape is reused from the AWS-managed cell because the absence is the same KIND of
    # absence -- decided, not failed -- and the column is sized for a chip. The reason is its own.
    return (
        '<span class="nodet nabadge" title="A brief describes what breaks between two'
        " versions of a component. There is no move here for one to describe: no recorded"
        " version series reaches this Kubernetes minor, so nothing was researched because"
        ' there is nothing this component could move to.">'
        "Brief: N/A</span>"
    )


def _decision_cell(
    step: StepRow,
    form: dict[str, dict] | None,
    *,
    sub: str | None = None,
    withheld: str | None = None,
) -> str:
    """Approve and reject for one step, or the reason there is neither.

    A CLUSTER HOP CARRIES NO CONTROL AND THE CELL SAYS SO IN PLAIN WORDS. This automation
    upgrades add-ons; it does not touch a control plane, and a step of that kind appears in a
    plan only as the boundary the add-on moves are placed around. The earlier wording --
    "Not authorisable", "a kind this system does not perform" -- was true and told a reader
    nothing they could act on: it named a property of the software rather than answering "so
    who advances my cluster, then". The answer is that they do, themselves, and that nothing
    on this page will do it for them if they leave it. A blank cell would read as an oversight
    and a button would be a control that cannot work, which this project refuses everywhere.

    WITHOUT FORMS the cell states what it would carry. That is the local-render path -- no
    bucket, nothing to presign -- and a button that posts nowhere is the silently-failing
    control this project refuses everywhere else.

    `sub` OVERRIDES THE LINE UNDER THE BUTTONS, and exists for exactly one caller. "Authorises
    step N only" is the right scope note for a move: it tells a reader that pressing this
    changes nothing about the other rows. It is the wrong note for an unsupported-at-target row,
    where the thing a reader must understand before pressing is not the scope but the MEANING --
    that they are accepting an unsupported component on this cluster and not clearing a path to
    the target. Passed in rather than branched on the kind here, so this function keeps one
    behaviour and the wording stays with the row that needs it.
    """
    if step.kind == "CLUSTER_UPGRADE":
        return (
            '<span class="muted nodet">You perform this one</span>'
            '<div class="sub">This automation upgrades add-ons only. Advancing the '
            "control plane is a recommendation for you to carry out yourself, so there is "
            "nothing here to approve and nothing here will do it.</div>"
        )
    if withheld:
        # A STEP THE CLUSTER CANNOT TAKE YET GETS NO BUTTON, AND THE CELL SAYS WHEN IT WILL.
        # Consider kube-proxy `v1.36.0-eksbuild.25` on a 1.35 cluster: when the plan
        # places that optional upgrade AT Kubernetes 1.36, AWS publishes that build for 1.36
        # only, and EKS refuses the call -- correctly -- as `InvalidParameterException`. A
        # page that offered a control for a change the cluster could not accept would make the
        # person pay for it with a failed run. So the control is withheld here, where the reason is
        # legible, rather than refused later by EKS where it is not. The same `.nabadge` the
        # page already uses for "AWS qualifies this add-on", because it is the same kind of
        # thing: a fact about the row, not a control.
        return (
            f'<span class="nodet nabadge" title="{_e(withheld)}">Not yet available</span>'
            f'<div class="sub">{_e(withheld)}</div>'
        )
    if not form:
        return (
            '<span class="muted nodet">No controls rendered</span>'
            '<div class="sub">no presigned form for this step</div>'
        )
    buttons = "".join(
        _decision_form(form[action], label, action)
        for action, label in (("approve", f"Approve step {step.order}"),
                              ("reject", f"Reject step {step.order}"))
        if action in form
    )
    return (
        f'<div class="btns">{buttons}</div>'
        f'<div class="sub dsub">{sub or f"authorises step {step.order} only"}</div>'
    )


def _decision_form(form: dict, label: str, action: str) -> str:
    """A REAL BUTTON, on the same terms as the fact-approval page's.

    The hidden fields are a presigned S3 POST policy minted by the stage, so the browser
    submits an authenticated write while holding no credential of its own, and S3 honours
    the policy's own expiry -- unlike a Lambda function URL, which caps signature freshness
    at five minutes however long you ask for. What the form may do is pinned by the policy
    to writing ONE object at ONE key carrying this run and this nonce, so it cannot be
    redirected at another step.

    `file` is a hidden text input rather than a picker: S3 takes that field's value as the
    object body, which is how a plain form writes a few bytes with no upload UI.
    """
    hidden = "".join(
        f'<input type="hidden" name="{_e(k)}" value="{_e(str(v))}">'
        for k, v in form["fields"].items()
    )
    return (
        f'<form class="act" action="{_e(form["url"])}" method="post" '
        'enctype="multipart/form-data">'
        f'{hidden}<input type="hidden" name="file" value="{_e(action)}">'
        f'<button class="btn btn-{action}" type="submit">{_e(label)}</button>'
        "</form>"
    )


# ---------------------------------------------------------------------------
# THE BRIEF, AS CHIPS
#
# EVERY PART OF IT IS A CHIP AND EXACTLY ONE OPENS. The chips are a radio group, so
# opening one closes whichever was open -- by definition, with no JavaScript and no state
# to keep. Independent disclosures let a reader open eleven panels and then scroll past
# their own trail looking for the one they wanted, which is the failure this shape avoids.
#
# THE CHIP ROW NEVER MOVES. Chips are labels in their own container and the panels sit
# below it, so the row a reader is aiming at stays where it was when they clicked.
#
# NOTHING IS SHOWN UNTIL IT IS ASKED FOR. `-none` is checked, so a freshly opened brief is
# a row of coins. A radio cannot be unchecked, so every panel carries a close control
# pointing back at `-none` -- otherwise "nothing shown" would be a state a reader could
# leave and never return to.
#
# ORDER AND NAMES ARE AN OPERATOR'S, NOT THE CONTRACT'S. The document's section names
# describe what the agent produced; these describe what a person about to run
# `helm upgrade` is asking, in the order they ask it: what do I run, then what bites me,
# then what I could pick up while I am in there, then how far to trust any of it.
_PANEL_NAMES: dict[str, tuple[str, str]] = {
    # section -> (chip label, the third column's heading in its table)
    "BREAKING_CHANGES": ("Breaking changes", "What changed"),
    "DEPRECATIONS": ("Deprecated", "What changed"),
    "RELEASE_NOTES": ("Release notes", "What changed"),
    "KNOWN_ISSUES": ("Known issues", "What is broken"),
    "NICE_TO_HAVES": ("Worth enabling", "What you get"),
}

_PHASE_WORDS: dict[str, str] = {
    "BEFORE_UPGRADE": "before",
    "DURING_UPGRADE": "during",
    "AFTER_UPGRADE": "after",
}


def _slug(value: str) -> str:
    """A section name as an id fragment. Stable, so a link to a panel keeps working."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _chip(group: str, sid: str, label: str, count: str, kind: str) -> str:
    """One coin, and THE RADIO IMMEDIATELY BEFORE IT that records it as the open one.

    THE RADIO LEADS, AND THAT ORDER IS THE WHOLE REASON THE SELECTED CHIP CAN BE DRAWN AS
    SELECTED. CSS has no previous-sibling selector, so while the label came first no rule
    could reach back from `:checked` to the coin the reader had just clicked -- the only
    highlight expressible was a static class, which is on whether you clicked it or not.
    That is why `Runbook` looked permanently open and the chip a reader actually chose
    looked untouched. With the input first, `input.ftab:checked + label.chip` IS the chosen
    coin, by adjacency: no id in a selector and no ordinal to keep in step with the markup.

    `count` is drawn apart from the label so the number can wear its own ink.
    """
    tail = f' <span class="n">{_e(count)}</span>' if count else ""
    classes = ("chip " + kind).strip()
    return (
        f'<input class="ftab" type="radio" name="{group}" id="{group}-{sid}">'
        f'<label class="{classes}" for="{group}-{sid}">{_e(label)}{tail}</label>'
    )


def _panel(group: str, label: str, count: str, body: str) -> str:
    """One panel, revealed by the radio two siblings before it.

    ADJACENCY IS STILL THE MECHANISM, now one hop longer: the chip's own label sits between
    the radio and this panel, so `input.ftab:checked + label.chip + .panel` couples them
    with one generic rule and no id in the selector. Emitted immediately after its chip,
    which is what keeps that chain contiguous -- so a maker must never emit the two apart.
    """
    tail = f' <span class="n">{_e(count)}</span>' if count else ""
    return (
        '<div class="panel"><div class="ptitle">'
        f'{_e(label)}{tail}'
        f'<label class="pclose" for="{group}-none">&#10005; close</label></div>'
        f'<div class="p">{body}</div></div>'
    )


def _finding_rows(findings: tuple[AdviceFinding, ...], heading: str) -> str:
    """One section's findings as a table.

    FOUR COLUMNS, NOT FIVE: the chip that opened this panel already named the section, so a
    section column would repeat it on every row.

    THE DETAIL IS CLAMPED, for `_clamp`'s reason applied to a longer field. A brief's detail
    runs to a paragraph, and rendered in full one cell sets the height of the whole row --
    which is what made an earlier version of this page unreadable. The full text stays in
    `title`, so nothing is lost; it is just no longer allowed to set the row's height.
    """
    rows = []
    for number, finding in enumerate(findings, start=1):
        if finding.issue_url:
            where = (
                f'<a href="{_e(finding.issue_url)}" rel="noreferrer noopener">'
                f"#{_e(str(finding.issue_number))}</a>"
                if finding.issue_number
                else _e(finding.release)
            )
        else:
            where = _e(finding.release)
        # Escaped per fragment and joined with the raw separator. Escaping the joined
        # string instead turns the separator into a literal `&amp;middot;`, and
        # un-escaping it afterwards would also un-escape any `&` a fragment really had.
        bits = [_e(b) for b in (finding.concern, _clamp(finding.detail, 240)) if b]
        for label, value in (
            ("affects", finding.affects),
            ("workaround", finding.workaround),
            ("helps", finding.why_it_helps),
            ("enable", finding.enablement),
        ):
            if value:
                bits.append(f"{label}: {_e(_clamp(value, 110))}")
        helm = "".join(
            f'<pre class="helm">{_e(snippet)}</pre>'
            for snippet in (finding.helm_values, finding.helm_set)
            if snippet
        )
        title_attr = (
            f' title="{_e(finding.detail)}"'
            if len(" ".join(finding.detail.split())) > 240
            else ""
        )
        basis = (
            '<span class="b-stated">STATED</span>'
            if finding.stated
            else '<span class="b-implied">IMPLIED</span>'
        )
        rows.append(
            f"<tr><td>{number}</td><td>{where}</td>"
            f'<td><div class="ft">{_e(finding.title)}</div>'
            f'<div class="fd"{title_attr}>{" &middot; ".join(bits)}</div>'
            + f"{helm}</td><td>{basis}</td></tr>"
        )
    return (
        '<table class="find"><thead><tr><th></th><th>Release</th>'
        f"<th>{_e(heading)}</th><th>Basis</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _source_list(panel: AdvicePanel) -> str:
    """Where a section was read, at the end of that section.

    Printed for an EMPTY section too, where it is where the brief LOOKED. An absence is
    only credible if it can show where it looked, and a NOT_LOOKED panel with no sources at
    all -- which happens -- says so rather than showing an empty list as though that were
    the same thing.
    """
    if not panel.sources:
        return (
            '<div class="psrc"><span class="psrc-h">Read from</span>'
            '<p class="muted">No page is recorded for this section.</p></div>'
        )
    items = "".join(
        f'<li><a href="{_e(_browse_url(link.url))}" rel="noreferrer noopener">'
        f"{_e(link.label)}</a></li>"
        for link in panel.sources
    )
    return (
        '<div class="psrc"><span class="psrc-h">Read from</span>'
        f'<ul class="srclist">{items}</ul></div>'
    )


def _panel_for(group: str, panel: AdvicePanel) -> tuple[str, str]:
    """One section as `(chip, panel)`.

    THE STATE DECIDES THE CHIP'S INK AND ONLY ONE OF THE THREE IS GREEN. REPORTED is
    neutral -- it is content, not a verdict. NONE_FOUND is the only absence the document
    lets a reader treat as reassurance, so it is the only green thing here. NOT_LOOKED is
    amber, because "we could not look" must never be mistakable for "we looked and it was
    clean".
    """
    label, heading = _PANEL_NAMES.get(panel.section, (_label(panel.section).title(), "What changed"))
    sid = _slug(panel.section)
    if panel.state == "REPORTED":
        return (
            _chip(group, sid, label, str(len(panel.findings)), ""),
            _panel(
                group,
                label,
                str(len(panel.findings)),
                _finding_rows(panel.findings, heading) + _source_list(panel),
            ),
        )
    if panel.state == "NONE_FOUND":
        body = (
            f"<p>{_e(panel.reason)}</p>"
            "<p><b>This is a read result and may be treated as reassurance</b> &mdash; "
            "unlike a not-looked section, which cannot be.</p>"
        ) + _source_list(panel)
        return (
            _chip(group, sid, label, "none found", "ok"),
            _panel(group, label, "none found", body),
        )
    body = (
        "<p><b>Not readable as reassurance.</b></p>"
        f"<p>{_e(panel.reason)}</p>"
        "<p>Anything here is <b>unknown rather than absent</b>.</p>"
    ) + _source_list(panel)
    return (
        _chip(group, sid, label, "not looked", "warn"),
        _panel(group, label, "not looked", body),
    )


def _runbook_panel(group: str, brief: StepAdvice) -> tuple[str, str] | None:
    """What to do, in order. The answer, so it wears the accent and comes first.

    `StepAdvice.runbook` arrives already sorted before-then-during-then-after, so it is
    printed as a runbook rather than re-ordered here. The phase is a tag on the line it
    belongs to; an earlier version put the phases in a left rail, which forced every
    instruction to start a third of the way across and made four short lines look like a
    form.
    """
    lines = brief.runbook
    if not lines:
        return None
    items = "".join(
        f'<li><span class="ph">{_e(_PHASE_WORDS.get(f.action_phase, ""))}</span>'
        f"<b>{_e(f.title)}</b> "
        f'<span class="why">{_e(_clamp(f.action or "", 300))}</span></li>'
        for f in lines
    )
    return (
        _chip(group, "runbook", "Runbook", str(len(lines)), "do"),
        _panel(group, "Runbook", str(len(lines)), f'<ol class="run">{items}</ol>'),
    )


def _chart_panel(group: str, brief: StepAdvice) -> tuple[str, str] | None:
    if not brief.chart_version:
        return None
    body = (
        f"<p>Install <code>{_e(brief.chart_version)}</code>.</p>"
        "<p>Stated separately because for most CNCF add-ons the chart number and the "
        "application number are different, and the plan names the application version "
        "&mdash; that is what the compatibility fact is about.</p>"
    )
    return (
        _chip(group, "chart", "Chart version", brief.chart_version, ""),
        _panel(group, "Chart version", brief.chart_version, body),
    )


def _path_panel(group: str, brief: StepAdvice) -> tuple[str, str] | None:
    """Whether the hop may be taken directly, or must stop on the way.

    A HOP WITH STOPS IS AMBER, because it contradicts the plan. The plan sequenced one
    move; the project's own documentation says several are required. An operator who finds
    that after starting has already broken it.
    """
    path = brief.path
    if path is None:
        return None
    if path.direct:
        body = (
            "<p><b>Nothing must be installed on the way.</b></p>"
            f"<p>{_e(path.detail)}</p>"
        )
        return (
            _chip(group, "path", "Upgrade path", "direct", ""),
            _panel(group, "Upgrade path", "direct", body),
        )
    stops = f"{len(path.must_install_in_order)} stops"
    chain = " &rarr; ".join(_e(stop) for stop in path.must_install_in_order)
    body = (
        f"<p><b>This is not a single hop.</b> {len(path.must_install_in_order)} release(s) "
        "must be installed on the way, in order:</p>"
        f'<div class="path">{chain}</div>'
        f"<p>{_e(path.detail)}</p>"
    )
    return (
        _chip(group, "path", "Upgrade path", stops, "warn"),
        _panel(group, "Upgrade path", stops, body),
    )


def _coverage_panel(group: str, brief: StepAdvice) -> tuple[str, str] | None:
    """How much of the jump was read, and what was missed.

    `releases_on_path` IS ZERO FOR AN ADD-ON WITH NO RELEASE LISTING, which is every
    EKS-managed one in practice. "5 of 0" is nonsense, so the chip says "5 read" instead --
    the denominator is only printed when there is one.
    """
    coverage = brief.coverage
    if coverage is None:
        return None
    read, on_path = len(coverage.releases_read), coverage.releases_on_path
    if coverage.minors_unread:
        chip = f"{read} read, {len(coverage.minors_unread)} missed"
        kind = "warn"
    else:
        chip = f"{read} of {on_path}" if on_path else f"{read} read"
        kind = ""
    parts = [
        f"<p>Read <b>{read}</b> release(s)"
        + (f" of the <b>{on_path}</b> on the way" if on_path else "")
        + ", crossing minor(s) <code>"
        + _e(" ".join(coverage.minors_entered))
        + "</code>.</p>"
    ]
    if coverage.minors_unread:
        parts.append(
            '<p><span class="gap">Minor(s) '
            + _e(" ".join(coverage.minors_unread))
            + " were not read</span> &mdash; anything they introduced is <b>not</b> in "
            "this brief, so the counts above are a floor rather than a total.</p>"
        )
    else:
        parts.append(
            '<p><span class="ok">Nothing was missed</span>, so the counts above are '
            "complete rather than a floor.</p>"
        )
    if coverage.releases_read:
        parts.append(
            '<div class="path">'
            + " &rarr; ".join(_e(r) for r in coverage.releases_read)
            + "</div>"
        )
    return (
        _chip(group, "coverage", "Coverage", chip, kind),
        _panel(group, "Coverage", chip, "".join(parts)),
    )


def _sources_panel(group: str, brief: StepAdvice) -> tuple[str, str]:
    links = brief.sources
    items = "".join(
        f'<li><a href="{_e(_browse_url(link.url))}" rel="noreferrer noopener">'
        f"{_e(link.label)}</a></li>"
        for link in links
    )
    body = (
        "<p>Everything above was read from these pages.</p>"
        f'<ul class="srclist">{items}</ul>'
        if links
        else '<p class="muted">No page is recorded for this step.</p>'
    )
    count = str(len(links))
    return (
        _chip(group, "sources", "Sources", count, ""),
        _panel(group, "Sources", count, body),
    )


def _about_panel(group: str, brief: StepAdvice, run_observed: str) -> tuple[str, str]:
    """That the brief gates nothing, and how old it is.

    BOTH HALVES MATTER AND THE SECOND IS THE EASY ONE TO DROP. A brief is stored under the
    upgrade it describes and served again for up to a week, so a reader may be looking at a
    days-old reading of an issue tracker. A cached answer indistinguishable from a fresh one
    is the whole hazard of reuse.
    """
    fresh = brief.observed_at[:10] == run_observed[:10]
    chip = "read fresh" if fresh else f"read {brief.observed_at[:10]}"
    body = (
        "<p>A brief tells you what an upgrade does to you. It <b>never gates the "
        "approval</b> and changes no version or verdict in this report &mdash; you can "
        "approve a move with no brief.</p>"
        f"<p>Read {_e(brief.observed_at[:16].replace('T', ' '))} UTC. "
        f"{_e(brief.reuse_note)}. Open issues change daily and projects edit their notes, "
        "so reading the same upgrade again may give a different answer.</p>"
    )
    return (
        _chip(group, "about", "About this brief", chip, ""),
        _panel(group, "About this brief", chip, body),
    )


def _advice_fields(brief: StepAdvice) -> str:
    """One step's whole brief: a row of coins, and the panels they open.

    NOTHING PRODUCED AT ALL IS ITS OWN SHAPE, not five empty panels. Five NOT_LOOKED panels
    say "we know nothing here"; five empty ones would say "we checked and it is clean",
    which is the lie this branch exists to avoid.
    """
    group = f"b{brief.step_order}"
    pairs: list[tuple[str, str]] = []

    if brief.nothing_was_produced:
        reason = next((p.reason for p in brief.panels if p.reason), "")
        body = (
            "<p><b>No section of this brief could be produced.</b> Nothing was read, so "
            "nothing is known about what this move breaks, deprecates or changes.</p>"
            f"<p>{_e(reason)}</p>"
            "<p>What this move breaks is <b>unknown, not absent</b> &mdash; which is a "
            "different answer from a brief that looked and found nothing. The verdict "
            "behind the move is unaffected: you may approve without a brief, or reject and "
            "ask for one.</p>"
        )
        pairs.append(
            (
                _chip(group, "nobrief", "No brief", "nothing read", "warn"),
                _panel(group, "No brief", "nothing read", body),
            )
        )
    else:
        for maker in (_runbook_panel, _chart_panel, _path_panel):
            made = maker(group, brief)
            if made is not None:
                pairs.append(made)
        pairs.extend(_panel_for(group, panel) for panel in brief.panels)
        coverage = _coverage_panel(group, brief)
        if coverage is not None:
            pairs.append(coverage)
        pairs.append(_sources_panel(group, brief))

    pairs.append(_about_panel(group, brief, brief.observed_at))

    # EACH FIELD IS EMITTED WHOLE -- radio, coin, panel -- AND THAT IS WHY THE PANELS NOW
    # LIVE INSIDE THE CHIP CONTAINER. Panels used to be collected after the container, which
    # kept them off the chip row but put the coin before its own radio, and CSS cannot look
    # backwards. So the three are kept contiguous instead, and the chip row is held together
    # by `order` on `.chips > .panel` rather than by where the panel sits in the markup:
    # a layout property does the job the document order was doing, and the selector chain
    # `input:checked + label.chip + .panel` becomes expressible.
    fields = "".join(chip + panel for chip, panel in pairs)
    return (
        '<div class="fields">'
        f'<input class="ftab" type="radio" name="{group}" id="{group}-none" checked>'
        f'<div class="chips">{fields}</div></div>'
    )


def _recommended_block(view: ReportView) -> str:
    if not view.recommended_upgrades:
        return ""
    body = "".join(
        "<tr>"
        f'<td><span class="name">{_e(r.component)}</span></td>'
        f"<td>{_version(r.from_series, emphasis=False)}</td>"
        f"<td>{_version(r.recommended_version, emphasis=True)}</td>"
        f"<td>Kubernetes {_e(r.at_kubernetes)}</td>"
        "</tr>"
        for r in view.recommended_upgrades
    )
    return (
        '<details class="card">'
        '<summary><span class="sum-title">Upgrade recommended</span>'
        '<span class="sum-sub">optional: already compatible, but a newer '
        'supported version exists</span>'
        f'<span class="count">{view.totals.upgrade_recommended}</span></summary>'
        '<div class="scroll"><table class="cols4">'
        "<thead><tr><th>Component</th><th>Installed series</th>"
        "<th>Recommended</th><th>Apply at</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div></details>"
    )


def _unresolved_row(row: UnresolvedRow) -> str:
    return (
        "<tr>"
        f'<td><span class="name">{_e(row.component)}</span>'
        f"{_where(row.component, None, row.workload_ref)}</td>"
        f"<td>{_verdict_pill(row.verdict)}</td>"
        f"<td>{_e(_ORIGIN_NOTES[row.origin])}</td>"
        f'<td class="src">{_e(row.reason)}</td>'
        "</tr>"
    )


def _unresolved_block(view: ReportView) -> str:
    """The section whose count a renderer can otherwise get wrong several ways.

    One list, one count, and the count in the pill is the same field the tile
    reads -- `view.totals.unresolved` -- because there is only one field.
    """
    totals = view.totals
    if not view.unresolved:
        body = (
            '<tr><td colspan="4" class="muted">Nothing unresolved: every '
            "component was decided, and everything decided was sequenced."
            "</td></tr>"
        )
    else:
        body = "".join(_unresolved_row(r) for r in view.unresolved)
    split = (
        f"{totals.unresolved_undecided} undecided"
        f" &middot; {totals.unresolved_unsequenceable_only} sequenced-blocked only"
    )
    return (
        '<details class="card" id="unresolved">'
        '<summary><span class="sum-title">Unresolved</span>'
        f'<span class="sum-sub">{split}</span>'
        f'<span class="count">{totals.unresolved}</span></summary>'
        '<p class="note">Gaps in recorded knowledge, not upgrade blockers. A '
        "component with no compatibility fact is reported as unknown rather than "
        "guessed at; verify each against its source before acting. A component here "
        "was either never researched, or was researched and the proposal was refused "
        "or left undecided at the approval stage -- either way nothing about it is "
        "asserted.</p>"
        '<div class="scroll"><table class="cols4">'
        "<thead><tr><th>Component</th><th>Status</th><th>Why it is here</th>"
        "<th>Reason</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div></details>"
    )


def _store_errors_block(view: ReportView) -> str:
    if not view.store_errors:
        return ""
    items = "".join(f"<li>{_e(e)}</li>" for e in view.store_errors)
    return (
        '<div class="banner">'
        '<strong>The fact store was degraded when this report was produced.</strong> '
        "A component below may read as having no fact when the fact merely would "
        f"not parse.<ul>{items}</ul></div>"
    )


def _tiles(view: ReportView) -> str:
    """The headline row. Every derived value is read from the validated totals.

    EVERY tile is now a validated total. There used to be an "Awaiting approval" tile
    fed by the length of a carried input rather than derived from the four stage
    documents, and it was the one number on this page a renderer was handed instead of
    reading. Approval happens before this report renders, so the count is always zero
    and the exception is gone with it.
    """
    totals = view.totals
    specs = [
        ("Detected", totals.detected, None),
        ("Compatible", totals.compatible, _GOOD),
        ("Upgrade needed", totals.upgrade_needed, _WARNING),
        ("Upgrade recommended", totals.upgrade_recommended, None),
        ("Unresolved", totals.unresolved, _NEUTRAL),
    ]
    return "".join(
        f'<div class="tile" style="--hue:{hue or "var(--accent)"}">'
        f'<div class="tile-n">{n}</div>'
        f'<div class="tile-l">{_e(label)}</div></div>'
        for label, n, hue in specs
    )


_CSS = """
/* DARK IS THE DEFAULT, not a media-query branch. This report is read on a
   projector, and a presenter's laptop set to light mode must not silently swap the
   deck out from under the talk. The accent is v1's orange (#f9ab00): the one hue
   that is not reserved for a status, so it can mark structure -- the eyebrow, the
   section stripes, the digest chain -- without a reader mistaking it for a verdict. */
:root {
  color-scheme: dark;
  --surface-0:#0f1115; --surface-1:#171a21; --surface-2:#20242d;
  --text-primary:#eef1f6; --text-secondary:#b9c0cc; --text-muted:#8f97a5;
  --border:#2a2f3a; --border-strong:#3b4250;
  --accent:#f9ab00;
  /* The warning step is a LIGHTER amber than the accent on purpose. v1 used one
     value for both, so its orange section stripes and its "upgrade needed" pills
     were the same hue and structure read as state. Two steps apart keeps the four
     status roles reserved for state, which is the rule the rest of this file obeys. */
  --status-good:#34a853; --status-warning:#ffd166;
  --status-serious:#ec835a; --status-critical:#ea4335;
  --shadow:0 1px 2px rgba(0,0,0,.5), 0 8px 24px rgba(0,0,0,.45);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:"Amazon Ember",-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
/* THERE IS DELIBERATELY NO `prefers-color-scheme: light` BRANCH, and that absence is
   the whole difference between "dark by default" and "dark". A media query would hand
   the choice back to the reading machine: headless Chrome reports light, and so does a
   presenter's laptop that has never been switched over -- so the deck would silently
   render light on exactly the screen it was designed for. v1 was unconditionally dark
   and that is the behaviour being restored.

   Light is still fully specified below, and reachable, but only by an explicit
   `data-theme="light"` stamp from a host page that has decided it wants one. Each step
   is chosen against its own surface rather than lightened from the dark one, and the
   accent darkens to #b37800 because #f9ab00 on white is a 1.8:1 text contrast. */
:root[data-theme="light"] {
  color-scheme: light;
  --surface-0:#f5f5f2; --surface-1:#fcfcfb; --surface-2:#ecece7;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#6f6d67;
  --border:#dcdbd4; --border-strong:#c6c5bc;
  --accent:#b37800;
  --status-good:#0ca30c; --status-warning:#8a6116;
  --status-serious:#c25a2c; --status-critical:#d03b3b;
  --shadow:0 1px 2px rgba(0,0,0,.06), 0 6px 18px rgba(0,0,0,.06);
}

* { box-sizing:border-box; }
body { margin:0; background:var(--surface-0); color:var(--text-primary);
       font-family:var(--sans); line-height:1.5; -webkit-font-smoothing:antialiased; }
.wrap { max-width:78rem; margin:0 auto; padding:0 1.5rem 4rem; }

/* v1's header: a navy gradient closed by an orange rule. The gradient is stated in
   the dark palette's own values so a light reader gets a flat surface instead of a
   dark band, which is the one place a hard-coded gradient would leak. */
header { border-bottom:2px solid var(--accent);
         background:linear-gradient(135deg,var(--surface-1) 0%,var(--surface-2) 55%,
                                    var(--surface-1) 100%); }
header .inner { max-width:78rem; margin:0 auto; padding:2rem 1.5rem 1.6rem; }
.eyebrow { font-size:.7rem; letter-spacing:.12em; text-transform:uppercase;
           color:var(--accent); font-weight:800; }
h1 { margin:.5rem 0 .4rem; font-size:1.85rem; font-weight:700; letter-spacing:-.02em; }
.hop { display:flex; flex-wrap:wrap; align-items:center; gap:.5rem .8rem;
       color:var(--text-secondary); font-size:.95rem; }
.hop .k { font-family:var(--mono); color:var(--text-primary); font-weight:600; }
.arrow { color:var(--accent); font-weight:700; }

.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
         gap:.85rem; margin:1.4rem 0 1.6rem; }
.tile { background:var(--surface-1); border:1px solid var(--border);
        border-top:3px solid var(--hue); border-radius:12px; padding:.95rem 1rem;
        box-shadow:var(--shadow); }
.tile-n { font-size:2.1rem; font-weight:700; line-height:1; }
.tile-l { margin-top:.45rem; font-size:.72rem; text-transform:uppercase;
          letter-spacing:.06em; color:var(--text-secondary); font-weight:700; }

.chain { display:flex; flex-wrap:wrap; gap:.5rem; margin:0 0 1.6rem; }
.chain-item { display:flex; align-items:center; gap:.45rem; background:var(--surface-1);
              border:1px solid var(--border); border-radius:999px;
              padding:.28rem .7rem; font-size:.76rem; }
.chain-l { color:var(--text-secondary); }
.chain-item code, header code { font-family:var(--mono); font-size:.75rem;
              color:var(--text-primary); }

h2 { font-size:.78rem; text-transform:uppercase; letter-spacing:.08em;
     color:var(--text-secondary); margin:1.9rem 0 .7rem; font-weight:700; }
.lead { color:var(--text-secondary); font-size:.88rem; margin:-.35rem 0 1rem; }
.notice { margin:1rem 0 0; padding:.75rem .9rem; border-radius:8px; border:2px dashed var(--accent);
  background:rgba(249,171,0,.08); color:var(--text-primary); font-size:.9rem; line-height:1.55; }
.lead a, a { color:var(--accent); }
a:hover { text-decoration:underline; }

/* The orange left stripe is v1's, and it is what makes a long page scannable on a
   projector: every top-level section starts at the same x with the same mark. */
details.card { background:var(--surface-1); border:1px solid var(--border);
               border-left:4px solid var(--accent);
               border-radius:12px; margin:0 0 .8rem; box-shadow:var(--shadow);
               overflow:hidden; }
summary { list-style:none; cursor:pointer; display:flex; align-items:center;
          gap:.65rem; padding:.85rem 1.05rem; font-weight:700; }
summary::-webkit-details-marker { display:none; }
summary::before { content:"\\25B8"; color:var(--text-muted); font-size:.75rem;
                  transition:transform .15s ease; }
details[open] > summary::before { transform:rotate(90deg); }
summary:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
.sum-title { flex:0 0 auto; }
.sum-sub { color:var(--text-secondary); font-weight:400; font-size:.82rem; }
.count { margin-left:auto; border:1px solid var(--border-strong); border-radius:999px;
         padding:.08rem .62rem; font-size:.78rem; font-variant-numeric:tabular-nums;
         color:var(--text-secondary); background:var(--surface-2); }
.note { margin:0 1.05rem .8rem; color:var(--text-secondary); font-size:.84rem; }

.scroll { overflow-x:auto; border-top:1px solid var(--border); }
/* FIXED LAYOUT, and it is what stops the page needing a horizontal scroll. With `auto`
   the browser sizes columns to their widest unbreakable content, so one long citation
   title or one workload reference widened the whole table past the viewport -- and a
   reader then scrolls right to find the decision, which is the column that matters most.
   Fixed plus explicit shares below means every column wraps inside the page instead. */
table { border-collapse:collapse; width:100%; table-layout:fixed;
        font-variant-numeric:tabular-nums; }
th, td { padding:.62rem .9rem; text-align:left; vertical-align:top; font-size:.88rem;
         border-bottom:1px solid var(--border); }
thead th { background:var(--surface-2); font-size:.68rem; text-transform:uppercase;
           letter-spacing:.06em; color:var(--text-secondary); font-weight:700;
           white-space:nowrap; }
tbody tr:last-child td { border-bottom:none; }
.num { text-align:right; font-weight:700; width:2.5rem; }
.name { font-weight:600; }
/* The component column holds a Kubernetes workload reference, which is long and
   has no spaces. Give it room rather than letting it break mid-word: on a
   projector a reference wrapping mid-path reads as a rendering
   fault. `break-word` is the fallback for a reference longer than the column. */
/* SCOPED TO `table.cols6`, AND THE SCOPE IS THE FIX. These shares were written for the
   component table and applied to every `tbody td` on the page -- so the four-column
   sequence, recommended and unresolved tables each got a component layout: `#` claimed 16%
   while blank, ACTION was squeezed to 9% and wrapped one word per line, and the status pills
   overran into the next column. Nothing was wrong with the numbers; they were being asked of
   tables they were never measured for. */
table.cols6 tbody td:first-child { width:19%; overflow-wrap:anywhere; }
/* INSTALLED IS 15%, NOT 10%, because 10% could not hold `1.13.2-eksbuild.21`: under
   `table-layout:fixed` the column does not grow to its content, so the mono version pill was
   CLIPPED MID-STRING and read as a corrupt value rather than a narrow column. The 5% comes off
   the evidence column, which is prose and reflows. `overflow-wrap` on the pill is the belt to
   that braces: a longer build suffix wraps inside the pill instead of being cut. */
table.cols6 tbody td:nth-child(2), table.cols6 thead th:nth-child(2) { width:15%; }
table.cols6 tbody td:nth-child(2) .ver { overflow-wrap:anywhere; }
table.cols6 tbody td:nth-child(3), table.cols6 thead th:nth-child(3) { width:12%; }
table.cols6 tbody td:nth-child(4), table.cols6 thead th:nth-child(4) { width:13%; }
table.cols6 tbody td:nth-child(5), table.cols6 thead th:nth-child(5) { width:14%; }
table.cols6 tbody td:nth-child(6), table.cols6 thead th:nth-child(6) { width:27%; }

/* The four-column tables: a wide first column for a component or workload reference, then
   room for prose. */
table.cols4 tbody td:first-child, table.cols4 thead th:first-child
  { width:30%; overflow-wrap:anywhere; }
table.cols4 tbody td:nth-child(2), table.cols4 thead th:nth-child(2) { width:16%; }
table.cols4 tbody td:nth-child(3), table.cols4 thead th:nth-child(3) { width:22%; }
table.cols4 tbody td:nth-child(4), table.cols4 thead th:nth-child(4) { width:32%; }
/* The sequence table leads with an ordinal, which needs almost nothing. */
table.steps tbody td:first-child, table.steps thead th:first-child { width:6%; }
table.steps tbody td:nth-child(2), table.steps thead th:nth-child(2) { width:14%; }
table.steps tbody td:nth-child(3), table.steps thead th:nth-child(3) { width:60%; }
table.steps tbody td:nth-child(4), table.steps thead th:nth-child(4) { width:20%; }
/* A pill must never be squeezed narrower than its own label. */
td .pill, th .pill { white-space:nowrap; }
/* `word-break:break-word` was breaking short phrases mid-token as soon as a column got
   tight. `overflow-wrap:anywhere` still rescues a genuinely unbreakable string such as a
   workload path, but leaves a phrase with spaces to wrap at its spaces. */
.sub { color:var(--text-muted); font-size:.76rem; font-family:var(--mono);
       margin-top:.18rem; overflow-wrap:anywhere; }
/* The sources column holds several titled links, one per document. It is the widest
   free-text column on the page, so it gets a fixed share of the table rather than a
   max-width that only bites once the table has already overflowed -- and its items are
   a real list, so they wrap and break inside a title rather than running on. */
.src { width:26%; }
.src ul.cited { margin:0; padding:0; list-style:none; }
.src ul.cited li { margin:0 0 .3rem; padding-left:.7rem; position:relative;
       line-height:1.35; overflow-wrap:anywhere; }
.src ul.cited li:last-child { margin-bottom:0; }
/* The character itself, not a hex escape. `content:"\2013"` with no terminating space
   is ambiguous -- the browser consumed three digits and left the fourth as a literal,
   rendering a stray "3" before every citation. */
.src ul.cited li::before { content:"–"; position:absolute; left:0;
       color:var(--text-muted); }
.synopsis { margin-top:.3rem; color:var(--text-secondary); font-size:.79rem;
            font-variant-numeric:normal; }
.muted { color:var(--text-muted); }
/* The authoritative-source mark wears the secondary ink, not a hue. It used to wear
   the accent, which was fine while the accent was blue; now that the accent is
   orange it would sit beside an amber "upgrade needed" pill and read as a second
   status. Its meaning is carried by `title` and `aria-label`, never by colour. */
.star { color:var(--text-secondary); }

.pill { display:inline-flex; align-items:center; gap:.35rem; white-space:nowrap;
        border:1px solid color-mix(in srgb, var(--hue) 55%, transparent);
        background:color-mix(in srgb, var(--hue) 12%, var(--surface-1));
        color:var(--text-primary); border-radius:999px; padding:.13rem .6rem;
        font-size:.75rem; font-weight:700; font-variant-numeric:normal; }
.pill-glyph { color:var(--hue); font-weight:700; }
.ver { display:inline-block; font-family:var(--mono); font-size:.79rem;
       border:1px solid var(--border-strong); background:var(--surface-2);
       border-radius:6px; padding:.1rem .45rem; white-space:nowrap; }
.ver-move { border-color:color-mix(in srgb, var(--status-good) 55%, transparent);
            background:color-mix(in srgb, var(--status-good) 12%, var(--surface-1)); }

.banner { background:color-mix(in srgb, var(--status-serious) 12%, var(--surface-1));
          border:1px solid color-mix(in srgb, var(--status-serious) 55%, transparent);
          border-radius:12px; padding:.9rem 1.05rem; margin:0 0 1.2rem;
          font-size:.87rem; }
.banner ul { margin:.5rem 0 0; padding-left:1.1rem; }

/* ---- the approve / reject controls, in the component's own row ----------------
   THESE LOOK LIKE BUTTONS AND ARE NOT BUTTONS. Each is a native disclosure summary
   styled as a pill: it clicks like a button, and what it opens is the signed command
   for that decision. A real submit control would need a form, a form posts unsigned,
   and an AWS_IAM function URL refuses an unsigned POST at the authorizer -- so a real
   button here would be a control that silently fails. Green approves, red rejects, and
   the word is inside the pill so the colour is never the only signal.

   NOTE, and it cost a test failure: this comment is EMITTED into the page, so it must
   not contain the literal markup of a form control. A test asserts the page carries no
   `input`, `button`, `select` or `textarea` tag, and an explanatory comment naming one
   in angle brackets trips it -- correctly, because a substring test over a whole
   document cannot tell prose from markup. Describe them, do not spell them. */
.decide { display:flex; flex-direction:column; gap:.35rem; align-items:flex-start; }
/* The proposal, above the two pills. Sized down from the row's own text: it is
   supporting detail for a decision, not a heading, and at body size it fought the
   component name for attention. */
.prop { display:flex; flex-direction:column; gap:.18rem; font-size:.82rem;
  line-height:1.35; max-width:34rem; }
.prop-why, .prop-notes { opacity:.85; }
.prop-why summary, .prop-notes summary { cursor:pointer; }
.prop-why p { margin:.25rem 0 0; font-style:italic; }
.prop-notes ul { margin:.25rem 0 0 1rem; padding:0; }
.prop-notes li { margin:.12rem 0; }
/* Caveats and gaps are marked, not coloured like an error: they are things a
   competent answer says about itself, and styling them as failures would teach a
   reader to skip exactly the sentences worth reading. */
.prop-caveat { padding-left:.55rem; border-left:2px solid currentColor; opacity:.75; }
.prop-gap { padding-left:.55rem; border-left:2px dotted currentColor; opacity:.7; }
.prop-sources { font-size:.8rem; opacity:.85; }
.prop-sources summary { cursor:pointer; }
.prop-sources ul { margin:.25rem 0 0 1rem; padding:0; }
.prop-sources li { margin:.1rem 0; }
/* Approve and Reject sit side by side and never wrap apart: they are one choice, and a
   reject that had dropped onto its own line would read as a separate afterthought. */
.btns { display:flex; flex-wrap:nowrap; gap:.4rem; }
.btn-wrap { display:inline-block; }
.btn { list-style:none; cursor:pointer; display:inline-flex; align-items:center;
       gap:.3rem; white-space:nowrap; border-radius:8px; padding:.3rem .8rem;
       font-size:.76rem; font-weight:800; letter-spacing:.01em;
       border:1px solid color-mix(in srgb, var(--btn) 70%, transparent);
       background:color-mix(in srgb, var(--btn) 22%, var(--surface-1));
       color:var(--text-primary); transition:background .12s ease; }
.btn::-webkit-details-marker { display:none; }
.btn::marker { content:""; }
.btn::before { content:""; width:.5rem; height:.5rem; border-radius:50%;
       background:var(--btn); flex:0 0 auto; }
.btn:hover { background:color-mix(in srgb, var(--btn) 34%, var(--surface-1)); }
.btn:focus-visible { outline:2px solid var(--btn); outline-offset:2px; }
.btn-approve { --btn:var(--status-good); }
.btn-reject { --btn:var(--status-critical); }
/* THE DONE BUTTON NEEDS ITS OWN HUE OR IT IS NOT A BUTTON AT ALL. `.btn` builds its border and
   background from `color-mix(... var(--btn) ...)`, so a class with no `--btn` collapses both to
   nothing and renders as bare bold text -- which is exactly how it first shipped. The accent
   rather than a status hue: finishing is not a verdict about anything, it is the page's own
   action. */
.btn-done { --btn:var(--accent); }
details[open] > .btn { border-bottom-left-radius:0; border-bottom-right-radius:0; }
/* The command drops out of the row, so it needs the width the cell does not have. */
/* The command lives in its own full-width row, revealed by the pill above it. `:has()`
   is what links them with no script: the pill's row is the subject, and `+` keeps the
   match adjacent so one component's pill can never reveal another's command.

   THE @supports GATE IS THE HONEST FALLBACK. Without `:has()` these rows cannot be
   revealed on click, so they must not be hidden in the first place -- a pill that opens
   and shows nothing is exactly the silently-failing control this whole design refuses.
   So `display:none` applies ONLY where `:has()` works; everywhere else both commands
   are simply always visible. Verbose, and correct. */
@supports selector(tr:has(> td)) {
  tr.cmdrow { display:none; }
  tr.arow:has(details.wrap-approve[open]) + tr.cmdrow-approve { display:table-row; }
  tr.arow:has(details.wrap-reject[open]) + tr.cmdrow-approve + tr.cmdrow-reject {
    display:table-row; }
}
tr.cmdrow > td { background:var(--surface-0); border-top:1px solid var(--border-strong);
       padding:.7rem .9rem .85rem; }
.btn-note { margin:0 0 .4rem; color:var(--text-secondary); font-size:.78rem; }
pre.cmd { user-select:all; -webkit-user-select:all; cursor:copy;
          font-family:var(--mono); font-size:.72rem; line-height:1.55;
          background:var(--surface-2); border:1px solid var(--border-strong);
          border-left:4px solid var(--hue,var(--accent)); border-radius:8px;
          padding:.6rem .75rem; margin:0; overflow-x:auto;
          white-space:pre-wrap; word-break:break-all; color:var(--text-primary); }
pre.cmd:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.cmd-approve { --hue:var(--status-good); }
.cmd-reject { --hue:var(--status-critical); }
.decide-meta { display:flex; align-items:center; gap:.4rem; flex-basis:100%;
       margin-top:.15rem; }
td.dec { width:18%; }

/* ---- awaiting-approval markers ----------------------------------------------
   Three places, because a decision must be findable without opening anything: the
   tile at the top, the pill on the group's own bar (a group can be collapsed), and
   the dot beside the component's name once the group is open. */
.await-pill { margin-left:auto; border-radius:999px; padding:.1rem .62rem;
       font-size:.72rem; font-weight:800; white-space:nowrap;
       border:1px solid color-mix(in srgb, var(--accent) 60%, transparent);
       background:color-mix(in srgb, var(--accent) 18%, var(--surface-1));
       color:var(--accent); }
.await-pill + .count { margin-left:.45rem; }
.card-await { border-left-color:var(--accent); }
.await-dot { color:var(--accent); font-size:.7rem; vertical-align:middle; }
tr.row-await > td { background:color-mix(in srgb, var(--accent) 7%, transparent); }
code.uri { user-select:all; -webkit-user-select:all; cursor:copy; font-size:.74rem;
           overflow-wrap:break-word; word-break:break-all; }

footer { margin-top:2.4rem; padding-top:1.2rem; border-top:1px solid var(--border);
         color:var(--text-secondary); font-size:.79rem; }
@media print {
  body { background:#fff; }
  details.card { break-inside:avoid; box-shadow:none; }
  details:not([open]) > summary::before { content:"\\25B8"; }
  /* A command is unusable on paper and its digests are already in the chain. */
  pre.cmd { display:none; }
}
"""

# ---------------------------------------------------------------------------
# THE APPROVAL PAGE'S OWN RULES, IN THEIR OWN CONSTANT AND NOT IN `_CSS`.
#
# `render/approval.py` imports `_CSS` for the fact-approval page. Appending to it would
# change that page's bytes to carry rules it never uses -- harmless to look at, and still
# a change to a rendered artifact for no reason. Emitted only when `to_html` is given
# advice, so a report with no approval controls is byte-identical to what it was.
_ADVICE_CSS = """
/* The six-column sequence table, paced like the fact-approval table. */
table.seq tbody td:nth-child(1), table.seq thead th:nth-child(1) { width:4%; }
table.seq tbody td:nth-child(2), table.seq thead th:nth-child(2) { width:22%;
       overflow-wrap:anywhere; }
table.seq tbody td:nth-child(3), table.seq thead th:nth-child(3) { width:12%; }
table.seq tbody td:nth-child(4), table.seq thead th:nth-child(4) { width:15%; }
table.seq tbody td:nth-child(5), table.seq thead th:nth-child(5) { width:25%; }
table.seq tbody td:nth-child(6), table.seq thead th:nth-child(6) { width:22%; }
.nodet { font-size:.78rem; }
/* A NON-INTERACTIVE BADGE THAT MATCHES THE `Details` BUTTON'S FOOTPRINT. It is deliberately
   not a `.chip`: nothing opens, so it must not look pressable. Same height and padding, muted
   ink and a dashed edge, and `white-space:nowrap` so it can never wrap the row out of line --
   which is what the sentence it replaced did. `cursor:help` is the only affordance, because
   the reason is on the title attribute. */
/* THE DONE BAR. Set apart from the table on purpose: it is not a decision about a step, and a
   reader must not mistake it for one more row. Stacks on a narrow screen so the sentence never
   squeezes the button off the line. */
/* LOUD ON PURPOSE. This is the one thing a reader must know before they touch a control, and
   at note size under a card heading it read as small print. Accent edge, tinted panel, a step
   up in size and weight -- the same treatment the page gives a warning, because forgetting to
   press Done costs the whole run. */
.pausenote { margin:.2rem 0 1rem; padding:.8rem 1rem; font-size:.95rem; line-height:1.55;
  font-weight:600; color:var(--text-primary); border-radius:10px;
  border:1px solid color-mix(in srgb, var(--accent) 55%, transparent);
  border-left:4px solid var(--accent);
  background:color-mix(in srgb, var(--accent) 10%, var(--surface-2)); }
.pausenote strong { color:var(--accent); }
.donebar { display:flex; flex-wrap:wrap; align-items:center; gap:1rem;
  justify-content:space-between; margin-top:1rem; padding:.9rem 1rem;
  border:1px solid var(--border-strong); border-radius:10px; background:var(--surface-2);
  font-size:.86rem; color:var(--text-secondary); }
.donebar > div { flex:1 1 22rem; min-width:16rem; }
.nabadge { display:inline-block; white-space:nowrap; cursor:help;
  padding:.16rem .5rem; border:1px dashed var(--border); border-radius:999px;
  color:var(--text-muted); }
/* THE "NOT PROGRESS" INK. Every disqualifying line on an unsupported-at-target row wears it --
   under the component name, under the recorded release, and in the note above the table. In the
   critical hue rather than `--text-muted`, because a muted sub-line under a version number is
   precisely what a reader scanning a column of versions skips, and the one thing they must not
   skip here is that the number above it does not cover the target. Not a pill and not a badge:
   it is a sentence, and this project does not paint sentences as status tokens. */
.sub.nocover { color:var(--status-critical); font-weight:600; }
/* SAME WEIGHT AS `.pausenote`, DIFFERENT HUE, AND BOTH CAN APPEAR AT ONCE. They say different
   things -- one is "nothing happens until you press Done", this is "this plan does not reach the
   target whatever you press" -- and a reader needs both before touching a control. The critical
   edge distinguishes them at a glance so the second does not read as a restatement of the
   first. */
.notreached { margin:.2rem 0 1rem; padding:.8rem 1rem; font-size:.95rem; line-height:1.55;
  color:var(--text-primary); border-radius:10px;
  border:1px solid color-mix(in srgb, var(--status-critical) 55%, transparent);
  border-left:4px solid var(--status-critical);
  background:color-mix(in srgb, var(--status-critical) 10%, var(--surface-2)); }
.notreached strong { color:var(--status-critical); }

/* THE DETAILS TOGGLE AND THE ROW IT REVEALS. html.py's own command-row pattern: the row
   spans every column, drops out of the grid, and `:has()` on the row above reveals it with
   `+` keeping the match adjacent so one step's control cannot open another's.

   THE @supports GATE IS THE HONEST FALLBACK. Where `:has()` does not work the brief row is
   never hidden, so the toggle degrades to a no-op beside content already on screen --
   rather than to a control that opens nothing, which is the silently-failing button this
   project refuses everywhere else. */
details.det { display:inline-block; }
summary.detbtn { display:inline-flex; align-items:center; gap:.3rem; cursor:pointer;
       padding:.16rem .6rem; border-radius:999px; font-size:.76rem; font-weight:700;
       white-space:nowrap; list-style:none;
       border:1px solid color-mix(in srgb, var(--accent) 55%, transparent);
       background:color-mix(in srgb, var(--accent) 12%, var(--surface-1));
       color:var(--text-primary); }
summary.detbtn::-webkit-details-marker { display:none; }
summary.detbtn::before { content:"\\25B8"; color:var(--accent); font-size:.7rem;
       transition:transform .15s ease; }
details.det[open] > summary.detbtn::before { transform:rotate(90deg); }
summary.detbtn:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
tr.briefrow > td { background:var(--surface-0);
       border-top:1px solid var(--border-strong); padding:.85rem 1.05rem 1rem; }
@supports selector(tr:has(> td)) {
  tr.briefrow { display:none; }
  tr.step:has(details.det[open]) + tr.briefrow { display:table-row; }
}

/* Approve and reject stacked in the row's own last cell, as the fact-approval report does
   it -- so a decision is reachable without opening anything, and the brief is what you
   open when you want to know why. */
td.dec .btns { display:flex; flex-direction:column; align-items:stretch; gap:.3rem; }
td.dec .btn { justify-content:flex-start; padding:.38rem .7rem; font-size:.78rem;
       font:inherit; font-weight:800; cursor:pointer; border-radius:8px; text-align:left;
       display:inline-flex; align-items:center; gap:.35rem;
       border:1px solid color-mix(in srgb, var(--btn) 70%, transparent);
       background:color-mix(in srgb, var(--btn) 22%, var(--surface-1));
       color:var(--text-primary); transition:background .12s ease; }
td.dec .btn::before { content:""; width:.5rem; height:.5rem; border-radius:50%;
       background:var(--btn); flex:0 0 auto; }
td.dec .btn:hover { background:color-mix(in srgb, var(--btn) 34%, var(--surface-1)); }
td.dec .btn:focus-visible { outline:2px solid var(--btn); outline-offset:2px; }
td.dec .btn-approve { --btn:var(--status-good); }
td.dec .btn-reject { --btn:var(--status-critical); }
td.dec form.act { margin:0; }
td.dec .dsub { margin-top:.35rem; }

/* EVERY PART OF THE BRIEF IS A CHIP AND EXACTLY ONE OPENS. A radio group, not a set of
   disclosures: radios in one group are mutually exclusive by definition, so opening a chip
   closes whichever was open with no JavaScript and no state to keep. `name` is per step, so
   two steps' rows are independent.

   INVISIBLE RATHER THAN `display:none`: a hidden input is not focusable, and a keyboard
   user must be able to reach these.

   FIXED, NOT ABSOLUTE, AND THAT ONE WORD IS A DEFECT FIX. Parked at `left:-9999px` the
   radio was off-screen but still IN the page, so clicking a chip -- which focuses the
   radio the label points at -- made the browser scroll to reveal it, and the reader landed
   at the bottom of the document every time. A fixed element is positioned against the
   viewport, so it is always already in view and focusing it scrolls nothing. `:checked`
   adjacency is a DOM relationship and does not care where the box is painted, so every
   selector below is unaffected. `pointer-events:none` keeps a 1px box pinned at the
   viewport corner from ever swallowing a click meant for the page. */
input.ftab { position:fixed; top:0; left:0; width:1px; height:1px; opacity:0;
       pointer-events:none; }
.chips { display:flex; flex-wrap:wrap; gap:.4rem; }
/* SCOPED TO A DIRECT CHILD, because the panels now sit inside this container and each one
   carries a `label.pclose`. `.chips label` would draw that close control as a chip. */
.chips > label { display:inline-flex; align-items:center; gap:.35rem; cursor:pointer;
       padding:.2rem .65rem; border-radius:999px; font-size:.78rem; font-weight:700;
       white-space:nowrap; border:1px solid var(--border-strong);
       background:var(--surface-1); color:var(--text-primary); }
.chips > label:hover { border-color:var(--accent); }
.chips > label .n { font-variant-numeric:tabular-nums; color:var(--text-secondary);
       font-weight:800; }
/* `Runbook` wears an accent EDGE because it is the answer -- and only an edge, because the
   filled accent below now means "this is the one you have open". Those two were the same
   treatment, which is why `Runbook` read as permanently selected. NONE_FOUND is the only
   absence the document lets a reader treat as reassurance, so it is the only green chip.
   NOT_LOOKED is amber: "could not look" must never be mistakable for "looked and it was
   clean". */
/* NO STANDING HIGHLIGHT ON ANY CHIP, and this is the whole of the fix the accent edge only
   half-made. `Runbook` carried an accent border so it read as the open panel on every row --
   which is exactly the complaint: the selection never appeared to move because one chip was
   always lit. A chip's ONLY distinguishing state is now `:checked`. `.do` survives as a hook
   with no paint, so nothing downstream breaks and the intent stays recorded. */
.chips > label.do { }
.chips > label.ok { border-color:color-mix(in srgb, var(--status-good) 55%, transparent);
       color:var(--status-good); }
.chips > label.warn { border-color:var(--status-serious); color:var(--status-serious); }

/* THE OPEN CHIP IS DRAWN AS OPEN. The accent, not a status token: `_CSS` reserves the four
   status hues for state a verdict asserts and keeps the accent for structure, and "which
   panel am I looking at" is structure -- a reader must not read the highlight as a finding
   about the upgrade. Filled and ringed rather than merely tinted, so it cannot be confused
   with `.do`'s edge, and the ink is left to the cascade so a green or amber chip keeps its
   own state colour while it is open. */
input.ftab:checked + label.chip {
       border-color:var(--accent);
       background:color-mix(in srgb, var(--accent) 30%, var(--surface-1));
       box-shadow:0 0 0 2px color-mix(in srgb, var(--accent) 40%, transparent); }
/* The focus ring belongs on the coin, since the radio itself is a 1px invisible box. Only
   expressible now that the input precedes its label. */
input.ftab:focus-visible + label.chip { outline:2px solid var(--accent);
       outline-offset:2px; }

/* ONE GENERIC RULE REVEALS THE PANEL: radio, then the coin, then the panel, so
   `+ label.chip + .panel` couples them with no id in the selector and no ordinal to
   maintain.

   THE CHIP ROW STILL NEVER MOVES, which is the point of `order`. The panels are flex items
   of the chip container now, so a panel opened from the third of nine coins would break the
   row in half and shove the rest below it -- the reader's aim would shift under their
   click. `order:1` lays every panel out after every chip whatever its position in the
   markup, and `flex-basis:100%` gives it the whole line to itself. */
.panel { display:none; }
.chips > .panel { order:1; flex-basis:100%; }
input.ftab:checked + label.chip + .panel { display:block; margin-top:.75rem;
       border:1px solid var(--border-strong); border-radius:10px;
       background:var(--surface-1); padding:.7rem .85rem; }
.ptitle { display:flex; align-items:center; gap:.5rem; font-size:.82rem; font-weight:700;
       color:var(--text-primary); margin:0 0 .55rem; padding-bottom:.45rem;
       border-bottom:1px solid var(--border); }
.ptitle .n { font-variant-numeric:tabular-nums; color:var(--text-secondary); }
.pclose { margin-left:auto; cursor:pointer; font-size:.72rem; font-weight:700;
       color:var(--text-muted); border:1px solid var(--border-strong);
       border-radius:999px; padding:.08rem .5rem; white-space:nowrap; }
.pclose:hover { color:var(--text-primary); border-color:var(--status-critical); }
.p { font-size:.85rem; line-height:1.65; color:var(--text-secondary); }
.p b { color:var(--text-primary); }
.p p { margin:.45rem 0 0; }
.p p:first-child { margin-top:0; }
.p .ok { color:var(--status-good); font-weight:700; }
.p .gap { color:var(--status-serious); font-weight:700; }
.p .path { font-family:var(--mono); font-size:.8rem; color:var(--text-primary);
       margin-top:.3rem; overflow-wrap:anywhere; }

/* THE RUNBOOK, WITH THE PHASE ON THE LINE IT BELONGS TO. An earlier version put the
   phases in a left rail, which forced every instruction to start a third of the way
   across and made four short lines look like a form. */
ol.run { margin:0; padding-left:1.3rem; font-size:.9rem; line-height:1.6;
       color:var(--text-primary); }
ol.run li { margin:.35rem 0; }
ol.run li .why { color:var(--text-secondary); font-weight:400; }
.ph { display:inline-block; font-size:.6rem; font-weight:800; letter-spacing:.05em;
       border-radius:3px; padding:.05rem .32rem; margin-right:.35rem; vertical-align:.08em;
       text-transform:uppercase; background:var(--surface-2);
       border:1px solid var(--border-strong); color:var(--text-secondary); }

/* A findings table inside a panel. Four columns, not five: the chip already named the
   section. Explicit percentage shares because the page-wide `table-layout:fixed` cascades
   in here, and under that algorithm a column declared `auto` does not get the leftover --
   it is distributed across every column, which squeezed the one column anyone reads. */
table.find { border:1px solid var(--border); border-radius:8px; overflow:hidden;
       table-layout:fixed; margin:.15rem 0 0; }
table.find thead th { font-size:.62rem; }
table.find td, table.find th { padding:.42rem .6rem; font-size:.86rem;
       border-bottom:1px solid var(--border); }
table.find tbody tr:last-child td { border-bottom:none; }
table.find tbody td:nth-child(1), table.find thead th:nth-child(1) { width:3.5%;
       text-align:right; color:var(--text-muted); font-family:var(--mono);
       font-size:.73rem; padding-right:.3rem; }
table.find tbody td:nth-child(2), table.find thead th:nth-child(2) { width:9%;
       font-family:var(--mono); font-size:.76rem; color:var(--text-muted); }
table.find tbody td:nth-child(3), table.find thead th:nth-child(3) { width:78.5%; }
table.find tbody td:nth-child(4), table.find thead th:nth-child(4) { width:9%; }
.ft { font-weight:600; line-height:1.45; }
.fd { color:var(--text-secondary); font-size:.8rem; line-height:1.5; margin-top:.16rem; }
/* STATED / IMPLIED, and both describe THE SOURCE rather than the reader: whether the notes
   say it, or whether it follows from what they do say. "Judged" put the reader in the
   sentence and invited "judged by what", which is a question about the pipeline. */
.b-stated { font-size:.66rem; font-weight:800; color:var(--status-warning); }
.b-implied { font-size:.66rem; font-weight:700; color:var(--text-secondary);
       letter-spacing:.02em; }

/* Per-section sources, at the end of the section that used them. */
.psrc { margin-top:.7rem; padding-top:.5rem; border-top:1px solid var(--border); }
.psrc-h { font-size:.62rem; text-transform:uppercase; letter-spacing:.08em;
       color:var(--text-muted); font-weight:800; }
ul.srclist { margin:.4rem 0 0; padding-left:1.1rem; font-size:.83rem; line-height:1.7; }
ul.srclist li { margin:.1rem 0; }
pre.helm { margin:.3rem 0 0; font-family:var(--mono); font-size:.74rem; line-height:1.5;
       background:var(--surface-0); border:1px solid var(--border-strong);
       border-left:3px solid var(--status-good); border-radius:6px; padding:.45rem .6rem;
       overflow-x:auto; white-space:pre; user-select:all; }

@media print {
  /* A control is unusable on paper. */
  td.dec form.act, .pclose { display:none; }
  tr.briefrow { display:table-row; }
}
"""

def to_html(
    view: ReportView,
    provenance: Provenance,
    *,
    advice: AdviceView | None = None,
    forms: dict[int, dict[str, dict]] | None = None,
    # THE CONTROL THAT RESUMES THE RUN. Separate from `forms` because it is not a decision
    # about any step: `forms` is keyed by step order and this belongs to the page.
    done: dict | None = None,
    # STEPS WHOSE CONTROLS WERE WITHHELD, keyed by order, each with the sentence that says why.
    # The approval stage decides this (a step placed at a Kubernetes minor the cluster has not
    # reached); the renderer only draws the badge. See `_decision_cell`.
    withheld: dict[int, str] | None = None,
    # ONE SENTENCE UNDER THE HEADER, for the read-only readiness report: it says that nothing on
    # this page can be approved or applied because the deployment does not execute upgrades. The
    # approval page passes nothing here and renders exactly as before.
    notice: str | None = None,
) -> str:
    """Render the value set as one self-contained HTML document.

    THE RUN'S ANSWER, AND NOTHING TO DECIDE. The verdicts, the ordered plan, what could
    not be sequenced and why, and the digest chain. It takes no `ApprovalControls` any
    more: the `Approval` stage pauses the run BEFORE `Present`, so every decision is
    already made by the time this renders and a control here would be furniture.

    That returns the page to one source of truth -- every cell reads from the fact store
    through `ComponentRow`, where cells used to fall back to a pending proposal because
    the fact was not stored yet.
    """
    totals = view.totals
    feasible = _readiness_pill(view)
    view_digest = view.digest()
    groups = "".join(
        _group_block(view, group)
        for group in _GROUP_TITLES
    ) or (
        # One `class` attribute, not two: a duplicate attribute is silently dropped
        # by every parser, so the second one never applied and the empty-state text
        # rendered at full contrast instead of muted.
        '<div class="card muted" style="padding:1rem">'
        "<em>No components were detected.</em></div>"
    )
    hop_path = f' <span class="arrow">&rarr;</span> '.join(
        f'<span class="k">{_e(h)}</span>' for h in view.hops
    )
    # ONLY WHERE THERE IS SOMETHING TO SUBMIT. The read-only report carries no forms, so it
    # gets no script and no toast markup -- its bytes are untouched by all of this.
    script = _DECISION_SCRIPT if (forms or done) else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Upgrade readiness - {_e(view.cluster.name)} - Kubernetes \
{_e(view.target_kubernetes_version)}</title>
<style>{_CSS}{_ADVICE_CSS if advice is not None else ""}{_CONTROL_CSS if script else ""}</style>
</head>
<body>
<header><div class="inner">
  <div class="eyebrow">Amazon EKS &middot; add-on upgrade readiness</div>
  <h1>{_e(view.cluster.name)}</h1>
  <div class="hop">
    <span>Kubernetes</span> <span class="k">{_e(view.current_kubernetes_version)}</span>
    <span class="arrow">&rarr;</span>
    <span class="k">{_e(view.target_kubernetes_version)}</span>
    <span>&middot;</span> {feasible}
    <span>&middot;</span> <span>hops {hop_path}</span>
    <span>&middot;</span> <span>{_e(view.cluster.region or "")}</span>
  </div>
</div></header>

<div class="wrap">
  {f'<p class="notice">{_e(notice)}</p>' if notice else ""}
  <div class="tiles">{_tiles(view)}</div>

  {_store_errors_block(view)}

  <h2>Components &middot; {totals.detected} detected</h2>
  <p class="lead">Grouped by how each one was installed. Undecided components are
    collected under <a href="#unresolved">Unresolved</a> at the bottom, counted
    once each.</p>
  {groups}

  {_sequence_block(view, advice, forms, done, withheld)}
  {_recommended_block(view)}

  {_unresolved_block(view)}

  <footer>
    No model formed any verdict in this report. Verdicts come from rules over a
    digest-cited knowledge ledger; this page is a rendering of those verdicts and
    computes every number it displays exactly once.
  </footer>
</div>
{script}
</body>
</html>
"""

# THE ONLY JAVASCRIPT ON THIS PAGE, AND THE ONE PLACE THE NO-JS RULE BENDS.
#
# WHY IT HAS TO. A form POST navigates -- that is what HTML does -- so `success_action_redirect`
# sent a decider to a confirmation page and made them navigate back for every one of eight
# rows. And nothing could mark a used control: the grant behind it is single-use so a second
# press cannot double-approve, but a button that still looks live invites the press and then
# appears to fail. Both wants -- stay on the page, and show the control is spent -- are only
# reachable from script.
#
# PROGRESSIVE ENHANCEMENT, NOT A REWRITE. Every form remains a real, working presigned POST with
# its redirect intact. This intercepts `submit`; if the fetch fails for any reason it calls
# `form.submit()` and the browser does exactly what it did before. So a blocked script, an old
# browser or a saved copy of this page all still record decisions -- they just navigate.
#
# SAME-ORIGIN, WHICH IS WHY NO CORS RULE IS NEEDED. The page is opened from a presigned GET on
# `<bucket>.s3.amazonaws.com` and every form posts to that same host, so `fetch` is same-origin.
# Opened from `file://` the fetch fails and the fallback above carries it.
_DECISION_SCRIPT = """
<iframe name="decsink" id="decsink" title="decision sink"
        style="position:fixed;width:0;height:0;border:0;visibility:hidden"></iframe>
<div class="toast" id="toast" role="status" aria-live="polite">
  <span id="toastmsg"></span>
  <button type="button" id="toastx" aria-label="Close">&#10005;</button>
</div>
<script>
(function () {
  var toast = document.getElementById('toast');
  var msg = document.getElementById('toastmsg');
  var timer = null;
  document.getElementById('toastx').addEventListener('click', function () { hide(); });
  function hide() { toast.classList.remove('show'); }
  function show(text, kind) {
    msg.textContent = text;
    toast.classList.remove('ok', 'bad');
    toast.classList.add('show', kind);
    if (timer) { clearTimeout(timer); }
    // Long enough to read, short enough not to sit over the next row. A close button is
    // there for anyone who wants it gone sooner.
    timer = setTimeout(hide, 6000);
  }
  function spend(form, action) {
    var row = form.closest('tr');
    var cell = row ? row.querySelector('td.dec') : null;
    if (!cell) { return; }
    cell.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    var mark = document.createElement('div');
    mark.className = 'decided ' + (action === 'approve' ? 'approved' : 'rejected');
    mark.textContent = action === 'approve' ? '\u2713 Approved' : '\u2715 Rejected';
    cell.appendChild(mark);
  }
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.classList || !form.classList.contains('act')) { return; }
    var data = new FormData(form);
    var action = String(data.get('file') || '');
    event.preventDefault();
    form.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    fetch(form.action, { method: 'POST', body: data })
      .then(function (res) {
        if (!res.ok) { throw new Error('HTTP ' + res.status); }
        if (action === 'done') {
          show('Decisions recorded. The run is continuing.', 'ok');
          document.querySelectorAll('form.act button').forEach(function (b) {
            b.disabled = true;
          });
        } else {
          show('Decision recorded.', 'ok');
          spend(form, action);
        }
      })
      .catch(function () {
        // THE PAGE MUST NOT NAVIGATE, EVEN WHERE `fetch` IS REFUSED. It is refused whenever the
        // page's origin differs from the bucket's -- opened from `file://`, from a saved copy, or
        // from a regional S3 host -- and the first version answered that by submitting normally,
        // which threw the reader onto the confirmation page and made them navigate back. That is
        // the exact thing this change exists to remove.
        //
        // So the fallback posts into a hidden iframe. A form targeting an iframe is an ordinary
        // navigation OF THAT IFRAME, so no CORS is involved and no bucket configuration is
        // needed, and the page itself never moves.
        //
        // WE CANNOT READ A CROSS-ORIGIN IFRAME'S RESULT, so this path reports optimistically and
        // its wording says so -- "sent" rather than "recorded". The authoritative confirmation is
        // the one the pipeline itself uses: the decision object arriving in S3. A grant is
        // single-use, so an optimistic mark cannot cause a double write; the worst case is a mark
        // for a submission that failed, and the run's own summary then shows that step as
        // undecided rather than approved.
        form.target = 'decsink';
        form.submit();
        if (action === 'done') {
          show('Decisions sent. The run is continuing.', 'ok');
          document.querySelectorAll('form.act button').forEach(function (b) {
            b.disabled = true;
          });
        } else {
          show('Decision sent.', 'ok');
          spend(form, action);
        }
      });
  }, false);
})();
</script>
"""

# THE CONTROL CHROME, SHARED BY BOTH APPROVAL PAGES. Extracted from `_ADVICE_CSS` when the fact
# approval page took the same behaviour: both pages render `form.act` holding `button.btn` inside
# a `td.dec`, so one stylesheet and one script serve both and there is no second copy to drift.
# Kept out of `_CSS` because the read-only report has no controls and must not carry their paint.
_CONTROL_CSS = """
/* THE TOAST, AND THE ONLY JAVASCRIPT ON THIS PAGE. See `_DECISION_SCRIPT` for why the no-JS
   rule bends here and how the page still works without it. */
.toast { position:fixed; inset-inline:0; bottom:1.25rem; margin-inline:auto; width:max-content;
  max-width:calc(100% - 2rem); z-index:50; display:none; align-items:center; gap:.6rem;
  padding:.7rem 1.1rem; border-radius:999px; font-size:.9rem; font-weight:700;
  color:var(--text-primary); border:1px solid var(--border-strong);
  background:var(--surface-2); box-shadow:0 8px 28px rgba(0,0,0,.45); }
.toast.show { display:flex; }
.toast.ok { border-color:color-mix(in srgb, var(--status-good) 65%, transparent);
  background:color-mix(in srgb, var(--status-good) 16%, var(--surface-2)); }
.toast.bad { border-color:color-mix(in srgb, var(--status-critical) 65%, transparent);
  background:color-mix(in srgb, var(--status-critical) 16%, var(--surface-2)); }
.toast button { all:unset; cursor:pointer; font-weight:800; opacity:.75;
  padding:0 .1rem; color:inherit; }
.toast button:hover { opacity:1; }
/* A CONTROL THAT HAS BEEN USED MUST LOOK USED. The grant behind it is single-use, so a second
   press cannot double-approve -- but a button that still looks live invites the press and then
   appears to fail. */
.btn[disabled] { cursor:default; opacity:.4; filter:grayscale(1); }
.decided { font-size:.76rem; font-weight:800; letter-spacing:.02em; }
.decided.approved { color:var(--status-good); }
.decided.rejected { color:var(--status-critical); }
"""
