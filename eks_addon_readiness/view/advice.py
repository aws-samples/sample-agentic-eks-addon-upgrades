"""The upgrade brief, mapped into the value set the approval page is allowed to draw.

    in    one `brief/document.py::UpgradeBrief`
    out   one `view/model.py::AdviceView`

WHY A MAPPER EXISTS RATHER THAN THE PAGE READING THE DOCUMENT. `view/render/` may import
three relative modules -- `model`, `command`, `html` -- and `tests/view/test_render_boundary.py`
refuses anything else by name. The brief is exactly the kind of document that guard was
written about: it has five sections whose lengths a renderer could total for itself, and a
page that computed its own counts is the class of defect this whole layer exists to remove.
So the count is taken once, here, and the page draws what it is handed.

WHAT IS DELIBERATELY DROPPED, and this is the shape of the mapping rather than an oversight.
The document carries the apparatus that makes it checkable -- every finding's quote, the
verdict of checking that quote against bytes recorded before the agent read them, the page
digest, whether the release was inside the walk, the evidence session prefix. None of it
reaches the page. A reader deciding whether to upgrade needs to know what the upgrade does
and where that was read; how the pipeline satisfied itself is a question about the pipeline.
The document keeps all of it, so nothing is lost -- it is simply not this page's subject.

WHAT IS DELIBERATELY KEPT, against the same instinct. The three section states survive
unchanged, because collapsing NOT_LOOKED into "empty" is the one simplification that would
make the page lie: "we could not look" and "we looked and the project announced nothing"
are different answers and only the second is reassurance. `minors_unread` survives for the
same reason.

NO CLOCK OF ITS OWN. Every instant on the output is copied from the document. This module
does not read the time, so mapping the same brief twice produces the same `AdviceView`.
"""

from __future__ import annotations

from ..brief.document import ComponentBrief, UpgradeBrief
from .model import (
    AdviceCoverage,
    AdviceFinding,
    AdviceLink,
    AdvicePanel,
    AdvicePath,
    AdviceView,
    StepAdvice,
    UnadvisedStep,
)

# The order the page shows the sections in, which is an operator's order rather than the
# contract's. Breaking changes first because they are what stops an upgrade; the two
# absences that can be reassuring sit where a reader meets them after the risk.
#
# Named here rather than taken from `BriefSection`'s declaration order so that adding a
# section to the contract does not silently reorder a page nobody re-read.
PANEL_ORDER: tuple[str, ...] = (
    "BREAKING_CHANGES",
    "DEPRECATIONS",
    "RELEASE_NOTES",
    "KNOWN_ISSUES",
    "NICE_TO_HAVES",
)


def _words(enum_value: object) -> str:
    """A screaming-snake enum member as words: `HELM_VALUES` -> `helm values`.

    Lower-cased rather than title-cased: these sit inside a row's supporting line, and
    title case there competes with the finding's own title for the eye.

    >>> _words("RBAC_OR_IDENTITY")
    'rbac or identity'
    >>> _words(None)
    ''
    """
    return str(enum_value or "").replace("_", " ").lower()


def _link(source: object) -> AdviceLink:
    """One `Citation` as the page's link type.

    `label` is passed through verbatim. The brief agent writes it, and rewriting it here
    would put a second author on a string a reader is meant to recognise from the source
    list -- but see the note in the stage about labels that name an API endpoint rather
    than a page, which is a defect in what the agent recorded and is fixed there.
    """
    return AdviceLink(url=str(getattr(source, "url", "")), label=str(getattr(source, "label", "")))


def _finding(item: object) -> AdviceFinding:
    """One `BriefItem` as a row.

    `stated` is `ChangeBasis` reduced to its one useful bit. The document's own words for
    the two cases are STATED and INFERRED; the page says STATED and IMPLIED, because both
    of those describe THE SOURCE -- whether the notes say it, or whether it follows from
    what they do say -- while "inferred" invites "inferred by what", which is a question
    about the pipeline and not about the upgrade.
    """
    return AdviceFinding(
        title=str(item.title),
        detail=str(getattr(item, "detail", "") or ""),
        concern=_words(getattr(item, "concern", None)),
        stated=str(getattr(item, "basis", "")) == "STATED",
        release=str(getattr(item, "release", "") or ""),
        issue_url=getattr(item, "issue_url", None),
        issue_number=getattr(item, "issue_number", None),
        action=getattr(item, "action", None),
        action_phase=str(getattr(item, "action_phase", "NO_ACTION")),
        affects=getattr(item, "affects", None),
        workaround=getattr(item, "workaround", None),
        why_it_helps=getattr(item, "why_it_helps", None),
        enablement=getattr(item, "enablement", None),
        helm_values=getattr(item, "helm_values", None),
        helm_set=getattr(item, "helm_set", None),
    )


def _panels(brief: ComponentBrief) -> tuple[AdvicePanel, ...]:
    """The five sections in the page's order, each keeping its own state.

    A section the document does not carry is skipped rather than invented as empty.
    `ComponentBrief.one_view_per_section` guarantees all five are present, so in practice
    nothing is skipped -- but inventing a panel here would mean a contract change upstream
    silently produced an all-clear, which is the one failure this mapping must not have.
    """
    by_name = {str(view.section): view for view in brief.sections}
    panels: list[AdvicePanel] = []
    for name in PANEL_ORDER:
        view = by_name.get(name)
        if view is None:
            continue
        panels.append(
            AdvicePanel(
                section=name,
                state=str(view.state),
                reason=str(view.reason or ""),
                findings=tuple(_finding(item) for item in view.items),
                sources=tuple(_link(source) for source in view.sources),
            )
        )
    # Any section the contract grows that this module has not been told about, appended
    # rather than dropped. A finding a reader never sees because a name was added upstream
    # is worse than one shown out of order.
    for name, view in by_name.items():
        if name not in PANEL_ORDER:
            panels.append(
                AdvicePanel(
                    section=name,
                    state=str(view.state),
                    reason=str(view.reason or ""),
                    findings=tuple(_finding(item) for item in view.items),
                    sources=tuple(_link(source) for source in view.sources),
                )
            )
    return tuple(panels)


def _coverage(brief: ComponentBrief) -> AdviceCoverage | None:
    coverage = brief.coverage
    if coverage is None:
        return None
    return AdviceCoverage(
        releases_on_path=len(coverage.releases_on_path),
        releases_read=tuple(coverage.releases_read),
        minors_entered=tuple(coverage.minors_entered),
        minors_unread=tuple(coverage.minors_unread),
    )


def _path(brief: ComponentBrief) -> AdvicePath | None:
    sequencing = brief.sequencing
    if sequencing is None:
        return None
    return AdvicePath(
        must_install_in_order=tuple(sequencing.must_install_in_order),
        detail=str(sequencing.detail or ""),
    )


def advice_from_brief(brief: UpgradeBrief | None) -> AdviceView | None:
    """One `AdviceView`, or None when there is no brief document at all.

    NONE IS A REAL ANSWER AND NOT A FAILURE. The brief cannot fail a run, so a deployment
    with the brief stage switched off -- or a run whose assemble step could not write --
    reaches the approval page with nothing to show. The page then says so and the approval
    proceeds, which is the property the whole brief stage was built around. Returning an
    empty `AdviceView` instead would be indistinguishable from a brief that found nothing,
    and that is the one confusion this layer must not introduce.
    """
    if brief is None:
        return None
    return AdviceView(
        observed_at=str(brief.observed_at),
        steps=tuple(
            StepAdvice(
                step_order=component.step_order,
                component=str(component.component),
                chart_version=component.chart_version,
                coverage=_coverage(component),
                path=_path(component),
                panels=_panels(component),
                observed_at=str(component.observed_at),
                reuse_note=str(component.reuse_note),
            )
            for component in brief.briefs
        ),
        unadvised=tuple(
            UnadvisedStep(
                step_order=absent.step_order,
                component=str(absent.component),
                reason=str(absent.reason),
            )
            for absent in brief.unbriefed
        ),
    )
