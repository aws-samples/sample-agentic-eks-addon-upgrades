"""The upgrade approval mail names what its count is a count of.

WHAT WENT WRONG. `stages/upgrade_approval.py` passes `waiting=len(offered)`, and `offered` is
every APPROVABLE PLAN STEP -- `COMPONENT_MOVE`, `COMPONENT_UPGRADE_RECOMMENDED` and the
unsupported-at-target row. A step is not a component: one component can hold several steps, and
the four AWS-managed recommendations are steps too. On `my-eks-cluster` targeting
Kubernetes 1.36 that was nine steps over seven components, and the mail said

    Approval needed: 9 add-on change(s) on my-eks-cluster -> Kubernetes 1.36

which reads as nine add-ons on a cluster the reader knows has seven. The number was correct and
the sentence made it look wrong, which is the more expensive error: it teaches a reader that this
mail's arithmetic does not reconcile, on the one mail they are required to open.

WHAT THIS FILE PINS. That the two counts are different measurements and the mail says so when
they differ; that it does NOT print a second number it could not take, or one that would read as
a bug; and that `_components` counts DISTINCT components, because a true interleaved multi-hop is
two steps for one component and `order/collapse.py` deliberately keeps that shape.

NOT A REDESIGN. The noun stays "add-on", the subject keeps its prefix and shape, and no content
moves out of the page into the mail. The approval mail and the run report are being redesigned
separately; this is the arithmetic, not the voice.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eks_addon_readiness.notify.upgrade_email import build_upgrade_email
from eks_addon_readiness.stages.upgrade_approval import _components


def _mail(waiting: int, components: int):
    return build_upgrade_email(
        run_id="00000000-0000-4000-8000-000000000001",
        cluster="my-eks-cluster",
        target_kubernetes_version="1.36",
        page_url="https://example.test/page",
        waiting=waiting,
        components=components,
    )


# ---------------------------------------------------------------------------
# The sentence.
# ---------------------------------------------------------------------------


def test_the_live_run_says_nine_changes_to_seven_add_ons() -> None:
    """The shape the human read wrongly, now stating both numbers.

    Asserted on the subject AND both bodies, because a mail client may show any one of them and
    the misreading happened in a preview pane.
    """
    mail = _mail(waiting=9, components=7)
    assert mail.subject == (
        "Approval needed: 9 change(s) to 7 add-on(s) on my-eks-cluster "
        "-> Kubernetes 1.36"
    )
    assert "9 change(s) to 7 add-on(s)" in mail.text_body
    assert "9 change(s) to 7 add-on(s)" in mail.html_body
    assert "9 add-on change(s)" not in mail.html_body


def test_one_step_per_component_keeps_the_single_number() -> None:
    """Nothing to misread, and "7 change(s) to 7 add-on(s)" reads like a bug.

    This is also what the live run becomes once `order/collapse.py` removes the unforced
    intermediate moves: seven steps over seven components.
    """
    mail = _mail(waiting=7, components=7)
    assert mail.subject == (
        "Approval needed: 7 add-on change(s) on my-eks-cluster -> Kubernetes 1.36"
    )


def test_a_count_that_could_not_be_taken_is_never_printed() -> None:
    """`components=0` means "not counted", which must not become "0 add-ons"."""
    mail = _mail(waiting=9, components=0)
    assert mail.subject == (
        "Approval needed: 9 add-on change(s) on my-eks-cluster -> Kubernetes 1.36"
    )
    assert "0 add-on" not in mail.subject
    assert "0 add-on" not in mail.html_body
    assert "0 add-on" not in mail.text_body


def test_the_default_caller_shape_still_builds() -> None:
    """`components` is keyword-only with a default, so an existing call site is unaffected."""
    mail = build_upgrade_email(
        run_id="r",
        cluster="c",
        target_kubernetes_version="1.36",
        page_url="https://example.test/p",
        waiting=3,
    )
    assert "3 add-on change(s)" in mail.subject


def test_the_unbriefed_sentence_says_which_noun_it_is_about() -> None:
    """With two nouns in the ask, "N of these" no longer resolves. It names changes."""
    mail = build_upgrade_email(
        run_id="r",
        cluster="c",
        target_kubernetes_version="1.36",
        page_url="https://example.test/p",
        waiting=9,
        components=7,
        briefed=5,
        unbriefed=2,
    )
    assert "2 of those changes have no advisory brief" in mail.text_body
    assert "2 of those changes" in mail.html_body


# ---------------------------------------------------------------------------
# The count itself.
# ---------------------------------------------------------------------------


def _row(order: int, component: str | None):
    return SimpleNamespace(order=order, component=component)


def test_distinct_components_are_counted_not_steps() -> None:
    """Two steps for one component is one component.

    A true interleaved multi-hop is exactly this shape, so counting steps here would reproduce
    the defect one layer down.
    """
    view = SimpleNamespace(
        steps=(
            _row(1, "cert-manager"),
            _row(2, "cert-manager"),
            _row(3, "keda"),
            _row(4, "metrics-server"),
        )
    )
    assert _components(view, (1, 2, 3, 4)) == 3


def test_a_step_not_offered_is_not_counted() -> None:
    """The count is over the rows the mail is asking about, not over the whole view."""
    view = SimpleNamespace(steps=(_row(1, "keda"), _row(2, "coredns")))
    assert _components(view, (1,)) == 1


@pytest.mark.parametrize("component", [None, ""])
def test_a_row_with_no_component_is_not_a_phantom_add_on(component: str | None) -> None:
    view = SimpleNamespace(steps=(_row(1, "keda"), _row(2, component)))
    assert _components(view, (1, 2)) == 1


def test_a_view_that_cannot_be_read_counts_nothing_rather_than_raising() -> None:
    """The mail is the one thing in this stage that must never fail an actionable approval.

    0 is "not counted", and `_ask` prints the single-number sentence for it -- so an unreadable
    view degrades the mail by one number instead of losing it.
    """
    assert _components(SimpleNamespace(), (1,)) == 0
