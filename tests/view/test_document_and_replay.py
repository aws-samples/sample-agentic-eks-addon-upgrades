"""The report is a document, and replay reaches it.

These are assertions that could not be made about a report with no
clock-free digested presentation artifact: with nothing to reproduce there was
nothing to compare.
"""

from __future__ import annotations

import json

import pytest

from eks_addon_readiness.view import (
    DocumentMismatch,
    build_document,
    renderer_digest,
    ruleset_digest,
    verify_document,
)


def test_two_builds_are_byte_identical(assessment, plan, inventory, ledger):
    """No clock, no run identifier: the same inputs render the same bytes.

    The property every stage document since Stage 2 has, extended to the report.
    """
    first, first_html, first_md = build_document(assessment, plan, inventory, ledger)
    second, second_html, second_md = build_document(
        assessment, plan, inventory, ledger
    )
    assert first.canonical_json() == second.canonical_json()
    assert first.digest() == second.digest()
    assert first_html == second_html
    assert first_md == second_md


def test_the_document_cites_every_upstream_digest(assessment, plan, inventory, ledger):
    """Six inherited digests plus this stage's own two."""
    document, _, _ = build_document(assessment, plan, inventory, ledger)
    assert document.snapshot_digest == plan.snapshot_digest
    assert document.report_digest == inventory.digest()
    assert document.ledger_digest == ledger.digest()
    assert document.assessment_digest == assessment.digest()
    assert document.plan_digest == plan.digest()
    assert document.store_digest == plan.store_digest
    assert document.ruleset_digest == ruleset_digest()
    assert document.renderer_digest == renderer_digest()
    # And the two are genuinely different fingerprints, so a rendering change and
    # a value change are distinguishable.
    assert ruleset_digest() != renderer_digest()


def test_the_rendered_bytes_are_digested_inside_the_document(
    assessment, plan, inventory, ledger
):
    """The hole in an earlier implementation: rendering sat downstream of every digest.

    Here the HTML and Markdown digests are fields on the document, so `verify` can
    re-render and compare rather than take the bytes on trust.
    """
    import hashlib

    document, html, markdown = build_document(assessment, plan, inventory, ledger)
    assert document.html_digest == (
        "sha256:" + hashlib.sha256(html.encode("utf-8")).hexdigest()
    )
    assert document.markdown_digest == (
        "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    )


def test_the_json_view_is_the_document(assessment, plan, inventory, ledger):
    """So no field can be omitted from it, which is what an earlier JSON view did."""
    document, _, _ = build_document(assessment, plan, inventory, ledger)
    payload = json.loads(document.canonical_json())
    assert payload["stage"] == "present"
    # Every total is present, including the one an earlier `_fields()` never computed.
    assert set(payload["view"]["totals"]) >= {
        "detected",
        "unresolved",
        "unresolved_undecided",
        "unresolved_unsequenceable_only",
        "upgrade_needed",
        "upgrade_recommended",
        "one_way_moves",
    }


def test_verify_reproduces_a_stored_report(assessment, plan, inventory, ledger):
    document, _, _ = build_document(assessment, plan, inventory, ledger)
    result = verify_document(document, assessment, plan, inventory, ledger)
    assert result.reproduced
    assert result.values_reproduced
    assert result.html_reproduced
    assert result.markdown_reproduced
    assert not result.ruleset_changed
    assert not result.renderer_changed
    assert "Reproduced exactly" in result.detail


def test_a_document_cannot_misattribute_its_own_view(assessment, plan, inventory, ledger):
    """`view_digest` is checked against the embedded view, not merely stored.

    So a stored report cannot claim to be a rendering of numbers it does not
    contain -- which is the shape a stale or hand-edited report would take.
    """
    document, _, _ = build_document(assessment, plan, inventory, ledger)
    with pytest.raises(ValueError, match="misattribute its own numbers"):
        document.model_validate(
            {**document.model_dump(), "view_digest": "sha256:" + "0" * 64}
        )


def test_verify_reports_a_report_built_from_other_numbers(
    assessment, plan, inventory, ledger
):
    """A stored report over a different value set does not pass verification.

    Stands in for the case that actually matters -- a stored report whose numbers
    no longer match what the documents say. The stored report here is a real,
    self-consistent document; it is simply not this run's.
    """
    fewer = assessment.model_validate(
        {
            **assessment.model_dump(),
            "decisions": assessment.model_dump()["decisions"][:2],
        }
    )
    matching_plan = plan.model_validate(
        {
            **plan.model_dump(),
            "unsequenceable": [],
            "assessment_digest": fewer.digest(),
        }
    )
    stale, _, _ = build_document(fewer, matching_plan, inventory, ledger)
    assert stale.view.totals.unresolved == 0

    result = verify_document(stale, assessment, plan, inventory, ledger)
    assert not result.reproduced
    assert not result.values_reproduced
    # The rules did not change, so this is reported as a defect rather than as
    # expected drift -- the distinction `verify_report` makes for Stage 2.
    assert not result.ruleset_changed
    assert "DEFECT" in result.detail


def test_documents_from_different_runs_are_refused(
    assessment, plan, inventory, ledger
):
    """Rendering a mixture of runs would produce a report that lies about itself."""
    other_plan = plan.model_validate(
        {**plan.model_dump(), "assessment_digest": "sha256:" + "9" * 64}
    )
    with pytest.raises(DocumentMismatch, match="not from one run"):
        build_document(assessment, other_plan, inventory, ledger)


def test_cluster_text_is_escaped_in_html(assessment, plan, inventory, ledger):
    """Cluster and fact-store text reaches the page; it is escaped on the way."""
    hostile = plan.cluster.model_validate(
        {**plan.cluster.model_dump(), "name": '<script>alert("x")</script>'}
    )
    hostile_plan = plan.model_validate({**plan.model_dump(), "cluster": hostile})
    hostile_assessment = assessment.model_validate(
        {**assessment.model_dump(), "cluster": hostile}
    )
    hostile_plan = hostile_plan.model_validate(
        {
            **hostile_plan.model_dump(),
            "assessment_digest": hostile_assessment.digest(),
        }
    )
    _, html, _ = build_document(
        hostile_assessment, hostile_plan, inventory, ledger
    )
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
