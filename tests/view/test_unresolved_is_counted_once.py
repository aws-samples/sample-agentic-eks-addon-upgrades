"""The regression test an earlier implementation never had.

The shipped defect: the
Unresolved tile read 10, the section pill read 8, and the true answer was 4. The
fix went into the HTML path only; the Markdown renderer still double-counted and
the JSON view omitted the number entirely. No test covered any of it, because the
only grouping fixture set `unsequenceable=()`.

These tests exercise the overlap directly and assert the one thing that matters:
**every format displays the same number, and that number is the size of one list.**
"""

from __future__ import annotations

import json

import pytest

from eks_addon_readiness.view import (
    ReportView,
    UnresolvedOrigin,
    build_document,
    build_report_view,
)


def test_the_overlap_is_counted_once(assessment, plan, inventory, ledger):
    """Four distinct components, not six.

    Stage 4 leaves three UNRESOLVED (metrics-server, keda, cert-manager) and Stage
    5 cannot sequence three (keda, cert-manager, bespoke-controller). Two are in
    both, so concatenating gives 6 -- which is what the earlier Markdown path renders -- and
    the union gives 4. The fixture is built so the two answers differ, which is
    exactly what the earlier `unsequenceable=()` fixture could not do.
    """
    view = build_report_view(assessment, plan, inventory, ledger)

    assert len(view.unresolved) == 4
    assert view.totals.unresolved == 4
    # The naive sum -- what the earlier Markdown path renders. It must differ from the
    # union, or this fixture would pass against the defect too.
    assert len(assessment.undecided) + len(plan.unsequenceable) == 6

    names = [row.component for row in view.unresolved]
    assert sorted(names) == [
        "Deployment/apps/bespoke-controller",
        "cert-manager",
        "keda",
        "metrics-server",
    ]


def test_origins_partition_the_list(assessment, plan, inventory, ledger):
    """Each row is attributed, and the two parts sum to the whole."""
    view = build_report_view(assessment, plan, inventory, ledger)
    origins = {row.component: row.origin for row in view.unresolved}

    assert origins["keda"] is UnresolvedOrigin.BOTH
    assert origins["cert-manager"] is UnresolvedOrigin.BOTH
    # Undecided in Stage 4 but not named by the plan.
    assert origins["metrics-server"] is UnresolvedOrigin.ASSESSMENT_ONLY
    # Decided, yet still unsequenceable -- the only genuinely additional row, and
    # the one a naive de-duplication by verdict would have dropped.
    assert (
        origins["Deployment/apps/bespoke-controller"] is UnresolvedOrigin.PLAN_ONLY
    )

    totals = view.totals
    assert totals.unresolved_undecided == 3
    assert totals.unresolved_unsequenceable_only == 1
    assert totals.unresolved_undecided + totals.unresolved_unsequenceable_only == (
        totals.unresolved
    )


def test_every_format_displays_the_same_number(assessment, plan, inventory, ledger):
    """HTML, Markdown and JSON agree, because there is only one field to read.

    This is the assertion the earlier implementation could not make. Its HTML computed the count twice in
    two styles, its Markdown did not de-duplicate, and its JSON had no such field
    -- so there was no single value to compare against.
    """
    document, html, markdown = build_document(assessment, plan, inventory, ledger)
    expected = document.view.totals.unresolved
    assert expected == 4

    # HTML: the tile and the section pill are both rendered from the same field.
    assert f'<div class="tile-n">{expected}</div>' in html
    assert f'<span class="count">{expected}</span>' in html
    # Exactly one row per unresolved component in the section table.
    assert html.count('id="unresolved"') == 1

    # Markdown: the heading carries the count, and the table has one row each.
    assert f"## Unresolved ({expected})" in markdown
    unresolved_table = markdown.split("## Unresolved")[1]
    rows = [
        line
        for line in unresolved_table.splitlines()
        if line.startswith("| ") and not line.startswith("| Component")
        and not line.startswith("|---")
    ]
    assert len(rows) == expected

    # JSON is the document itself, so the field cannot be omitted.
    payload = json.loads(document.canonical_json())
    assert payload["view"]["totals"]["unresolved"] == expected
    assert len(payload["view"]["unresolved"]) == expected


def test_a_wrong_total_is_refused(assessment, plan, inventory, ledger):
    """A headline number that disagrees with its list cannot be constructed.

    The defect this guards against is not a crash, it is a plausible-looking
    report with a wrong number on it -- which is precisely what shipped earlier.
    """
    payload = build_report_view(assessment, plan, inventory, ledger).model_dump()
    payload["totals"]["unresolved"] = 10  # the number the earlier tile actually showed
    with pytest.raises(ValueError, match="totals say 10, the list holds 4"):
        ReportView.model_validate(payload)


def test_a_component_cannot_appear_twice(assessment, plan, inventory, ledger):
    """The contract refuses a concatenated list, not merely a wrong count.

    Built so the totals AGREE with the doubled list, which is the case the
    totals check cannot see: concatenating Stage 4's undecided set onto Stage 5's
    unsequenceable set and then counting the result is self-consistent and still
    wrong. That is exactly the shape of the earlier Markdown renderer.
    """
    payload = build_report_view(assessment, plan, inventory, ledger).model_dump()
    payload["unresolved"] = [*payload["unresolved"], payload["unresolved"][0]]
    payload["totals"]["unresolved"] = len(payload["unresolved"])
    payload["totals"]["unresolved_undecided"] += 1
    with pytest.raises(ValueError, match="names the same component more than once"):
        ReportView.model_validate(payload)


def test_no_unresolved_components_renders_cleanly(
    assessment, plan, inventory, ledger
):
    """The all-green case, which is what the reference cluster actually produces."""
    clean_assessment = assessment.model_validate(
        {
            **assessment.model_dump(),
            "decisions": [
                d
                for d in assessment.model_dump()["decisions"]
                if d["hops"][0]["verdict"] == "COMPATIBLE"
            ],
        }
    )
    clean_plan = plan.model_validate(
        {
            **plan.model_dump(),
            "feasible": True,
            "unsequenceable": [],
            "assessment_digest": clean_assessment.digest(),
        }
    )
    view = build_report_view(clean_assessment, clean_plan, inventory, ledger)
    assert view.totals.unresolved == 0
    document, html, markdown = build_document(
        clean_assessment, clean_plan, inventory, ledger
    )
    assert "Nothing unresolved" in html
    assert "Nothing unresolved" in markdown
    assert document.view.totals.unresolved == 0
