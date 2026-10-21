"""The sweep asks one question: has this component's GitHub repo cut a release since we read?

THE DEFECT THESE TESTS WERE WRITTEN AGAINST, taken from a live sweep document rather than
from reasoning. `sweeps/.../store-7f906e771769.rules-e5f17688fc38.sweep-c1982e2cfc2d.json`
recorded:

    component_id      metrics-server
    outcome           NOT_COMPARABLE
    pinned_revision   "master"
    upstream_revision null
    marked            []

The resolver did its half correctly -- for a branch ref it resolved the branch head to a
commit sha. But the FACT had recorded the ref NAME, `master`, as its pinned revision, so the
comparison was "master" against a sha: two different kinds of value that can never be equal.
All three of metrics-server's cited sources are in that state, so the staleness mechanism ran,
cost nothing, and could never fire.

WHAT REPLACES IT, and it is a narrower question rather than a repaired comparison: a release
is what can change a support window. A commit on the default branch is mostly CI and docs
churn, and marking on one would send a human back to re-approve facts that did not change. So
the sweep derives the component's GitHub repository, asks GitHub for its newest release, and
compares that release's `published_at` against the fact's own `refreshed_at`. Nothing needs to
have been recorded in advance, which is why the research side needed no change at all.

RED FIRST. Every assertion in `TestTheMetricsServerShapeGetsARealVerdict` fails against the
sha-comparison implementation, and it fails on the outcome rather than on a signature: the
metrics-server fact is pinned to a branch, so the old code skipped the comparison entirely and
answered NOT_COMPARABLE. That is the exact shape that must now get a real verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from eks_addon_readiness.knowledge import ComponentKnowledge
from eks_addon_readiness.research import freshness as rules
from eks_addon_readiness.research.marks import MarkKind, MarkLedger

# The real values, so a reader can check the verdict against the world. metrics-server's
# fact was approved on 2026-09-13 and its newest release at that point was 0.9.0, published
# on 2026-07-13 -- two months BEFORE the fact was read. The honest answer for this fact is
# therefore "no newer release, not stale", and a fix that says so is working.
MS_REPO = "kubernetes-sigs/metrics-server"
MS_REFRESHED = datetime(2026, 9, 13, 16, 50, 8, 14784, tzinfo=UTC)
MS_RELEASE_TAG = "v0.9.0"
MS_RELEASE_PUBLISHED = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "ab" * 32


def _metrics_server_shaped_fact(
    refreshed: datetime = MS_REFRESHED,
    *,
    component: str = "metrics-server",
    repository: str = f"https://github.com/{MS_REPO}",
    cited: tuple[str, str, str] = (
        f"https://github.com/{MS_REPO}/raw/master/README.md",
        f"https://github.com/{MS_REPO}/releases",
        f"https://github.com/{MS_REPO}/releases?page=2",
    ),
) -> ComponentKnowledge:
    """A fact shaped exactly like the stored metrics-server one: every pin a BRANCH name.

    Three cited sources, all recording `master` as the revision, one of them a repository
    page with no file path at all -- which is the state the live document reported and the
    state no sha comparison can ever act on.
    """

    def cite(url: str) -> dict[str, Any]:
        return {
            "url": url,
            "title": "the component's own compatibility table",
            "source_revision": "master",
            "retrieved_at": refreshed.isoformat(),
        }

    readme, releases, page_two = (cite(url) for url in cited)
    return ComponentKnowledge.model_validate(
        {
            "knowledge_base_revision": f"{component}-researched-2026-09-13",
            "canonical_id": component,
            "canonical_repository": repository,
            "official_documentation": [repository],
            "compatibility_rules": [
                {
                    "component_version_series": "0.9.x",
                    "kubernetes_versions": {
                        "minimum_inclusive": "1.30",
                        "maximum_exclusive": "1.40",
                    },
                    "evidence": [readme],
                }
            ],
            "releases": [
                {
                    "version": "0.9.0",
                    "released_at": MS_RELEASE_PUBLISHED.isoformat(),
                    "source_revision": "master",
                    "evidence": [releases],
                }
            ],
            "verification_checks": [
                {
                    "check_id": "serves-its-api",
                    "description": "The component serves its documented API.",
                    "evidence": [page_two],
                }
            ],
            "default_upgrade_strategy": "IN_PLACE",
            "refreshed_at": refreshed.isoformat(),
            "evidence_confidence": "HIGH",
        }
    )


def _off_github_fact() -> ComponentKnowledge:
    """A fact with no GitHub anywhere: cert-manager.io and keda.sh shaped, and out of scope."""
    return _metrics_server_shaped_fact(
        repository="https://gitlab.test/group/project",
        cited=(
            "https://cert-manager.io/docs/installation/supported-releases/",
            "https://keda.sh/docs/2.21/operate/cluster/",
            "https://gitlab.test/group/project/-/blob/main/README.md",
        ),
    )


def _release(tag: str, published_at: datetime) -> dict[str, Any]:
    return {"tag": tag, "published_at": published_at.isoformat()}


def _finding(fact: ComponentKnowledge, resolver: Any) -> rules.SweepFinding:
    """The finding the sweep's own document would carry for one fact."""
    result = rules.sweep((fact,), MarkLedger(), NOW, resolver=resolver)
    document = rules.sweep_document(
        result,
        mode="scheduled",
        swept_at=NOW,
        store_digest=DIGEST,
        marks_before=MarkLedger(),
    )
    assert len(document.findings) == 1
    return document.findings[0]


class TestTheMetricsServerShapeGetsARealVerdict:
    """THE RED TESTS. A branch-pinned fact with no sha anywhere must still be decidable."""

    def test_a_branch_pinned_fact_is_compared_rather_than_skipped(self) -> None:
        """NOT_COMPARABLE was the defect. The same fact must now come out UNMOVED.

        Nothing about the fact changed -- it still records `master` at every citation. What
        changed is the question: the sweep no longer needs a recorded sha, because it asks
        GitHub what the newest release is and compares that against when the fact was read.
        """
        fact = _metrics_server_shaped_fact()
        # Keyed by the literal `owner/repo` GitHub is asked for, which is also the shape the
        # event's `pinned_releases` map uses to freeze a sweep.
        resolver = rules.StaticUpstreamResolver(
            {MS_REPO: _release(MS_RELEASE_TAG, MS_RELEASE_PUBLISHED)}
        )

        finding = _finding(fact, resolver)

        assert finding.outcome is rules.SweepOutcome.UNMOVED, (
            "a fact pinned to a branch must still get a real verdict: the sweep asks about "
            "releases now, and needs no recorded sha to do it"
        )
        assert finding.repository == MS_REPO
        assert finding.release_tag == MS_RELEASE_TAG
        assert finding.release_published_at == MS_RELEASE_PUBLISHED.isoformat()
        assert finding.fact_refreshed_at == MS_REFRESHED.isoformat()

    def test_a_release_published_after_the_fact_was_read_marks_it(self) -> None:
        """The mechanism firing: a release the fact never saw withdraws the fact."""
        fact = _metrics_server_shaped_fact()
        newer = MS_REFRESHED + timedelta(days=2)
        resolver = rules.StaticUpstreamResolver({MS_REPO: _release("v0.9.1", newer)})

        result = rules.sweep((fact,), MarkLedger(), NOW, resolver=resolver)

        assert result.expired == ("metrics-server",)
        mark = result.new_marks[0]
        assert mark.kind is MarkKind.STALE
        # The mark is auditable on its own terms: repository, tag and both instants.
        assert MS_REPO in mark.reason
        assert "v0.9.1" in mark.reason
        assert newer.isoformat() in mark.reason
        assert MS_REFRESHED.isoformat() in mark.reason

    def test_the_finding_records_everything_needed_to_check_it_by_hand(self) -> None:
        """A human must be able to re-check the verdict without re-running the sweep."""
        fact = _metrics_server_shaped_fact()
        newer = MS_REFRESHED + timedelta(hours=1)
        resolver = rules.StaticUpstreamResolver({MS_REPO: _release("v0.10.0", newer)})

        finding = _finding(fact, resolver)

        assert finding.outcome is rules.SweepOutcome.MARKED_STALE
        assert finding.repository == MS_REPO
        assert finding.release_tag == "v0.10.0"
        assert finding.release_published_at == newer.isoformat()
        assert finding.fact_refreshed_at == MS_REFRESHED.isoformat()


class TestOnlyGitHubReleasesDecideIt:
    """The scope the user drew: GitHub releases, and nothing else."""

    def test_a_release_at_the_same_instant_as_the_read_is_not_newer(self) -> None:
        """The boundary is strict. Equal instants mean the fact already saw that release."""
        fact = _metrics_server_shaped_fact()
        resolver = rules.StaticUpstreamResolver(
            {MS_REPO: _release("v0.9.0", MS_REFRESHED)}
        )

        assert rules.is_stale(fact, resolver).stale is False

    def test_a_component_with_no_github_repository_is_not_comparable(self) -> None:
        """cert-manager.io and keda.sh are out of scope, so a fact citing only them cannot
        be decided -- and NOT_COMPARABLE says exactly that rather than guessing."""
        finding = _finding(_off_github_fact(), rules.StaticUpstreamResolver({}))

        assert finding.outcome is rules.SweepOutcome.NOT_COMPARABLE
        assert finding.repository is None
        # The reason must say what could not be DETERMINED, not why a sha did not match.
        assert "GitHub" in finding.reason
        assert "sha" not in finding.reason.casefold()

    def test_an_unreadable_upstream_never_marks(self) -> None:
        """The discipline that must survive: a bad minute at GitHub withdraws nothing.

        A repository with no releases at all lands here too, and that is correct -- "we
        could not establish a newest release" is not evidence that one was published.
        """
        fact = _metrics_server_shaped_fact()

        finding = _finding(fact, rules.StaticUpstreamResolver({}))

        assert finding.outcome is rules.SweepOutcome.NOT_COMPARABLE
        assert finding.repository == MS_REPO
        assert finding.release_tag is None
        assert "not stale" in finding.reason.casefold()

    def test_the_default_branch_moving_is_not_a_trigger(self) -> None:
        """No branch-head check, by decision: commits are CI and docs churn.

        Asserted over the resolver's own surface, because the cheapest way this creeps back
        in is a second question added beside the release one.
        """
        import inspect

        assert not hasattr(rules.GitHubUpstreamResolver, "current_revision")
        source = inspect.getsource(rules)
        assert "/commits/" not in source, (
            "a branch-head read is back: 'has master changed since yesterday' is true almost "
            "always on an active repository, so it would mark every fact on a CI tweak"
        )
        assert "/releases" in source


class TestTheDecisionDoesNotDependOnWhenItRuns:
    """The anti-TTL property, restated for a mechanism that compares two instants.

    `now` never enters the comparison: it is the fact's own read instant against upstream's
    publication instant. So the same fact and the same upstream state give the same verdict
    on any day, which is the property a 24-hour window destroyed.
    """

    def test_an_ancient_fact_whose_repo_has_not_released_is_not_stale(self) -> None:
        old = _metrics_server_shaped_fact(refreshed=MS_REFRESHED - timedelta(days=400))
        # A release published a year before the fact was read: still not newer.
        resolver = rules.StaticUpstreamResolver(
            {MS_REPO: _release("v0.8.0", MS_REFRESHED - timedelta(days=800))}
        )

        assert rules.is_stale(old, resolver).stale is False

    def test_the_same_inputs_give_the_same_verdict_whatever_the_sweep_clock_says(
        self,
    ) -> None:
        fact = _metrics_server_shaped_fact()
        resolver = rules.StaticUpstreamResolver(
            {MS_REPO: _release(MS_RELEASE_TAG, MS_RELEASE_PUBLISHED)}
        )
        first = rules.is_stale(fact, resolver)
        second = rules.is_stale(fact, resolver)

        assert first == second

    def test_neither_the_summary_nor_the_document_names_a_time_window(self) -> None:
        fact = _metrics_server_shaped_fact()
        resolver = rules.StaticUpstreamResolver(
            {MS_REPO: _release(MS_RELEASE_TAG, MS_RELEASE_PUBLISHED)}
        )
        result = rules.sweep((fact,), MarkLedger(), NOW, resolver=resolver)
        document = rules.sweep_document(
            result,
            mode="scheduled",
            swept_at=NOW,
            store_digest=DIGEST,
            marks_before=MarkLedger(),
        )
        body = document.canonical_json().casefold()

        assert "ttl" not in body
        assert "hours" not in body
        assert document.staleness == "upstream_release"


class TestTheRepositoryIsDerivedOnce:
    """One call per repository, cached for the sweep's life."""

    def test_two_facts_on_one_repository_cost_one_lookup(self) -> None:
        class _Counting:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def latest_release(self, repository: str) -> rules.UpstreamRelease | None:
                self.calls.append(repository)
                return rules.UpstreamRelease(
                    repository=repository,
                    tag=MS_RELEASE_TAG,
                    published_at=MS_RELEASE_PUBLISHED,
                )

        counting = _Counting()
        one = _metrics_server_shaped_fact(component="metrics-server")
        two = _metrics_server_shaped_fact(component="metrics-server-two")

        rules.sweep((one, two), MarkLedger(), NOW, resolver=counting)

        # The rules ask once per FACT; the caching belongs to the resolver, which is where
        # the network is. Both facts name the same repository, so the deployed resolver
        # turns these two asks into one request.
        assert counting.calls == [MS_REPO, MS_REPO]

    def test_the_github_resolver_caches_per_repository(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolver = rules.GitHubUpstreamResolver()
        calls: list[str] = []

        def _fetch(repository: str) -> rules.UpstreamRelease | None:
            calls.append(repository)
            return rules.UpstreamRelease(
                repository=repository,
                tag=MS_RELEASE_TAG,
                published_at=MS_RELEASE_PUBLISHED,
            )

        monkeypatch.setattr(resolver, "_fetch_latest_release", _fetch)

        assert resolver.latest_release(MS_REPO) is not None
        assert resolver.latest_release(MS_REPO) is not None
        assert resolver.latest_release("kubernetes-sigs/METRICS-server") is not None

        assert calls == [MS_REPO], "one request per repository, case-insensitively"

    def test_an_unreadable_repository_is_cached_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A None answer is cached, so a repo with no releases is not asked twice."""
        resolver = rules.GitHubUpstreamResolver()
        calls: list[str] = []

        def _fetch(repository: str) -> rules.UpstreamRelease | None:
            calls.append(repository)
            return None

        monkeypatch.setattr(resolver, "_fetch_latest_release", _fetch)

        assert resolver.latest_release(MS_REPO) is None
        assert resolver.latest_release(MS_REPO) is None

        assert calls == [MS_REPO]


class TestTheRepositoryComesFromTheFact:
    def test_the_canonical_repository_decides_it(self) -> None:
        fact = _metrics_server_shaped_fact()

        assert rules.github_repository(fact) == MS_REPO

    def test_a_citation_answers_when_the_canonical_repository_is_not_github(self) -> None:
        """keda's docs live in other repositories, so the canonical field is preferred --
        but a fact whose canonical repository is off GitHub may still cite one."""
        fact = _metrics_server_shaped_fact(repository="https://gitlab.test/group/project")

        assert rules.github_repository(fact) == MS_REPO

    def test_a_fact_with_no_github_anywhere_derives_nothing(self) -> None:
        assert rules.github_repository(_off_github_fact()) is None
