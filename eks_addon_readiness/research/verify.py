"""Verifying every stored value against a quote, and every quote against fetched bytes.

Verification here closes two holes that compound: a circular check, and one that covers
only a fraction of what gets stored.

**Circularity.** Checking `_value_in_quote(claim.value, claim.quote)` compares two strings
the model wrote in the same turn, so a model that invents a value and a matching quote
passes -- a six-line table quote line-spliced from two different commits of one README,
present in neither, passes such a check. So verification here is two-stage and the order
matters:

    1. the quote must appear byte-for-byte in the body of the page it cites
    2. the value must appear in the quote

Stage 1 is the one the model cannot influence, and it runs first. A quote that fails it
makes stage 2 meaningless, so the value is reported unverified regardless.

**Coverage.** Checking `extraction.claims` alone leaves the values that drive verdicts --
`extraction.rule`, `additional_rules`, `known_releases` and `recommended_version` --
unexamined, so a fact can reach HIGH on a single claim with a single quote while storing
rules and releases that trace to nothing.

So the unit of verification here is not the claim; it is **every value that will be
stored**. `verify_extraction` enumerates them from the statements and releases that
assemble into the fact, and a fact where any verdict-affecting value traces to no verified
quote cannot reach the live store.

**False negatives are the other half of the problem.** A naive check rejects
`kubernetes_tested_range='v1.33 - v1.35'` as absent from its source when it is verbatim
present, because a range string does not normalise as a version and falls through to a
case-sensitive substring test that re-typed whitespace breaks. A check that rejects truths
teaches its operators to ignore it. `value_in_quote` handles versions, ranges and free text
explicitly, and comparison is whitespace-insensitive on the value side while the
quote-versus-body check stays byte-exact -- the tolerance goes where a human retyping a
number is plausible, not where a splice must be caught.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .boundary import (
    BoundKind,
    ChartProvenanceInput,
    CompatibilityStatementInput,
    ResearchExtraction,
)
from .pages import PageSet

# Any `1.2`, `v1.2`, `1.2.3` token. Used to compare version-shaped values by value rather
# than by spelling, so `v1.27` in a quote satisfies a value of `1.27`.
_VERSION_TOKEN = re.compile(r"v?\d+(?:\.\d+)+")


def _is_sibling_query_page(cited_url: str, found_url: str) -> bool:
    """Whether two URLs are the SAME page addressed with a different query string.

    True only when the scheme, host and path are identical and the query differs. This is
    the one shape of mis-citation `check_quote` re-attributes rather than rejecting: a
    paginated upstream list -- `.../releases?page=3` versus `.../releases?page=2` -- where
    the model read the quote off one page of the list and cited another page of the SAME
    list. GitHub republishes those pages as releases are cut, so a quote whose bytes have
    since shifted from `?page=3` to `?page=2` is a real sighting with a stale page number,
    not a fabrication.

    Everything else that lands on the WRONG_PAGE path stays there. A different PATH
    (`.../releases` versus `.../tags`) is a different document, not another view of one
    list; a different HOST is a different project entirely; and the empty-query-both-sides
    case cannot arise here because an identical scheme/host/path with an identical (or
    absent) query is the same key, which `PageSet.get` would already have matched. The
    fragment is ignored -- it never reaches the server and so never changes the bytes.

    >>> _is_sibling_query_page(
    ...     "https://github.com/kubernetes-sigs/karpenter/releases?page=3",
    ...     "https://github.com/kubernetes-sigs/karpenter/releases?page=2",
    ... )
    True
    >>> _is_sibling_query_page(
    ...     "https://github.com/kubernetes-sigs/karpenter/releases",
    ...     "https://github.com/kubernetes-sigs/karpenter/tags",
    ... )
    False
    >>> _is_sibling_query_page(
    ...     "https://github.com/kubernetes-sigs/karpenter/releases?page=3",
    ...     "https://example.com/kubernetes-sigs/karpenter/releases?page=2",
    ... )
    False
    """
    cited, found = urlsplit(cited_url), urlsplit(found_url)
    return (
        (cited.scheme, cited.netloc, cited.path)
        == (found.scheme, found.netloc, found.path)
        and cited.query != found.query
    )


def _normalize_version(token: str) -> str:
    """`v1.27` -> `1.27`. Spelling-insensitive, value-preserving.

    >>> _normalize_version("v1.27")
    '1.27'
    >>> _normalize_version("1.27")
    '1.27'
    """
    return token.lstrip("vV")


def _collapse_space(text: str) -> str:
    """Collapse runs of whitespace to one space.

    Applied only to the value-in-quote comparison, never to the quote-in-body one.

    >>> _collapse_space("v1.33   -  v1.35")
    'v1.33 - v1.35'
    """
    return " ".join(text.split())


def value_in_quote(value: str, quote: str) -> bool:
    """Whether `value` is present in `quote`, by three explicit shapes.

    **A single version** matches on normalised value against every version token in the
    quote, so `1.27` is found in `v1.27` and is not found inside `1.271`.

    >>> value_in_quote("1.27", "supported from v1.27 onwards")
    True
    >>> value_in_quote("1.2", "supported from v1.27 onwards")
    False

    **A range or any value containing versions** matches when every version token in the
    value is present in the quote, whitespace collapsed. This is the false negative to
    avoid: a re-typed `v1.33 - v1.35` differing only in spacing being reported absent from
    a source that contains it.

    >>> value_in_quote("v1.33 - v1.35", "| v2.20 | v1.33   -   v1.35 |")
    True
    >>> value_in_quote("v1.33 - v1.36", "| v2.20 | v1.33 - v1.35 |")
    False

    **Free text** falls back to a whitespace-collapsed, case-insensitive substring test.
    Case-insensitive because a value copied out of a heading legitimately differs in case
    from its occurrence in a table cell, and rejecting that is another false negative with
    no compensating safety -- the quote itself is already pinned to fetched bytes.

    >>> value_in_quote("metrics.k8s.io/v1beta1", "| `Metrics.k8s.io/v1beta1` |")
    True
    >>> value_in_quote("policy/v1beta1", "| `metrics.k8s.io/v1beta1` |")
    False

    **A minor is contained in its own patch releases.** A chart's `kubeVersion: '>=v1.23.0-0'`
    states a floor of minor 1.23, and the statement's `stated_minimum` is spelled `1.23`
    because that field holds minors. The value was copied faithfully; only the spelling
    differs by the patch the constraint carries. Observed live: one such row cost a KEDA
    fact with 42 of 43 values verified its confidence. A patch is NOT found in a bare
    minor -- `1.23.0` is more than `1.23` says.

    >>> value_in_quote("1.23", "kubeVersion: '>=v1.23.0-0'")
    True
    >>> value_in_quote("1.23.0", "requires Kubernetes 1.23")
    False
    """
    value_tokens = _VERSION_TOKEN.findall(value)
    if value_tokens:
        quote_tokens = {_normalize_version(t) for t in _VERSION_TOKEN.findall(quote)}
        return all(_token_present(_normalize_version(t), quote_tokens) for t in value_tokens)
    return _collapse_space(value).casefold() in _collapse_space(quote).casefold()


def _token_present(token: str, quote_tokens: set[str]) -> bool:
    """Exact token, or a `major.minor` whose `major.minor.patch` the quote carries."""
    if token in quote_tokens:
        return True
    if token.count(".") == 1:
        return any(q.startswith(token + ".") for q in quote_tokens)
    return False


class QuoteCheck:
    """One quote judged against the bytes of the page it cites.

    `verdict` is one of:

    `PRESENT`
        The quote appears byte-for-byte in the cited page's body.
    `PRESENT_WHITESPACE`
        Absent byte-for-byte, present once whitespace is collapsed. Reported separately
        rather than folded into PRESENT: a retyped quote is plausible and not disqualifying,
        but a reader should know the bytes were not identical.
    `VERIFIED_SIBLING_PAGE`
        Absent from the cited page, but present verbatim in ANOTHER page fetched this run
        whose URL differs from the cited one ONLY in its query string (same scheme, host
        and path -- `.../releases?page=2` for a quote that cited `?page=3`). The quote is
        real and the page it came from WAS fetched; only the page number in the citation
        is stale, which is routine for a paginated upstream list that shifts as releases
        are published. Treated as verified and the citation is RE-ATTRIBUTED to the page
        whose bytes actually carry the quote (`attributed_url`/`attributed_revision`), so
        the stored fact cites the page it was read from. Distinct from PRESENT so the
        re-attribution is visible on the receipt and the approval page; distinct from
        WRONG_PAGE because that defect is not recoverable and this one is.
    `WRONG_PAGE`
        Present in some other page fetched in this run, but not in the one cited, and that
        other page is NOT a sibling query-page of it (a different path, a different host).
        The quote is real; the citation names a genuinely different document. A distinct
        verdict because it is a citation defect, not a fabrication -- but an unrecoverable
        one, because nothing says which of two unrelated pages the fact should cite.
    `ABSENT`
        Present in no page fetched in this run. This is what catches a splice.
    `NOT_FETCHED`
        No page was fetched at this exact URL and revision, so nothing can be compared.
        Never treated as a pass.

    `attributed_url`/`attributed_revision` are the page the quote should be CITED to.
    They equal `url`/`revision` for every verdict except `VERIFIED_SIBLING_PAGE`, where
    they name the sibling query-page the bytes were actually found on. The assembler reads
    them so the citation on the stored fact names the page that carries the quote, not the
    stale page number the model wrote.
    """

    PRESENT = "PRESENT"
    PRESENT_WHITESPACE = "PRESENT_WHITESPACE"
    VERIFIED_SIBLING_PAGE = "VERIFIED_SIBLING_PAGE"
    WRONG_PAGE = "WRONG_PAGE"
    ABSENT = "ABSENT"
    NOT_FETCHED = "NOT_FETCHED"

    def __init__(
        self,
        quote: str,
        url: str,
        revision: str,
        verdict: str,
        detail: str,
        page_digest: str | None = None,
        attributed_url: str | None = None,
        attributed_revision: str | None = None,
    ) -> None:
        self.quote = quote
        self.url = url
        self.revision = revision
        self.verdict = verdict
        self.detail = detail
        self.page_digest = page_digest
        # Where the quote should be cited. Equal to the cited URL/revision unless the
        # quote was re-attributed to a sibling query-page (VERIFIED_SIBLING_PAGE).
        self.attributed_url = attributed_url if attributed_url is not None else url
        self.attributed_revision = (
            attributed_revision if attributed_revision is not None else revision
        )

    @property
    def reattributed(self) -> bool:
        """Whether the citation was moved to a different page than the one cited."""
        return (self.attributed_url, self.attributed_revision) != (self.url, self.revision)

    @property
    def verified(self) -> bool:
        """Whether this quote may support a stored value.

        `PRESENT_WHITESPACE` counts. `VERIFIED_SIBLING_PAGE` counts: the quote is in
        fetched bytes and the citation is re-attributed to the page carrying them, so the
        value remains traceable from the fact. `WRONG_PAGE` does not: a value whose only
        support is a quote from a page the fact does not (and cannot be made to) cite is
        not traceable from the fact, which is what the citation is for.
        """
        return self.verdict in (
            self.PRESENT,
            self.PRESENT_WHITESPACE,
            self.VERIFIED_SIBLING_PAGE,
        )


def check_quote(quote: str, url: str, revision: str, pages: PageSet) -> QuoteCheck:
    """Judge one quote against the fetched bytes of the page it cites."""
    page = pages.get(url, revision)
    if page is None:
        return QuoteCheck(
            quote,
            url,
            revision,
            QuoteCheck.NOT_FETCHED,
            (
                f"no page was fetched at {url} @ {revision or '(unpinned)'}, so this "
                "quote could not be checked against any bytes"
            ),
        )

    if page.contains(quote):
        return QuoteCheck(
            quote,
            url,
            revision,
            QuoteCheck.PRESENT,
            "the quote appears byte-for-byte in the cited page",
            page.digest,
        )

    if _collapse_space(quote) in _collapse_space(page.body):
        return QuoteCheck(
            quote,
            url,
            revision,
            QuoteCheck.PRESENT_WHITESPACE,
            (
                "the quote appears in the cited page once whitespace is collapsed; the "
                "bytes differ only in spacing"
            ),
            page.digest,
        )

    elsewhere = pages.any_contains(quote)
    if elsewhere is not None:
        if _is_sibling_query_page(url, elsewhere.url):
            # SAME list, different page number. The bytes moved between `?page=N` views as
            # releases were published; the sighting is real. Verified, and the citation is
            # re-attributed to the page the quote was actually found on so the stored fact
            # points at bytes that contain it. The verdict names both URLs so the receipt
            # shows the move.
            return QuoteCheck(
                quote,
                url,
                revision,
                QuoteCheck.VERIFIED_SIBLING_PAGE,
                (
                    f"the quote is absent from the cited page {url} but present in "
                    f"{elsewhere.url} @ {elsewhere.revision or '(unpinned)'}, which is the "
                    "same list addressed by a different query string; the citation is "
                    "re-attributed to the page whose bytes carry the quote"
                ),
                elsewhere.digest,
                attributed_url=elsewhere.url,
                attributed_revision=elsewhere.revision,
            )
        return QuoteCheck(
            quote,
            url,
            revision,
            QuoteCheck.WRONG_PAGE,
            (
                f"the quote is absent from the cited page but present in "
                f"{elsewhere.url} @ {elsewhere.revision or '(unpinned)'}; the citation "
                "does not name the page this text came from"
            ),
            page.digest,
        )

    return QuoteCheck(
        quote,
        url,
        revision,
        QuoteCheck.ABSENT,
        (
            f"the quote appears in NO page fetched in this run ({len(pages)} page(s) "
            "fetched). It was not read from any source; it was composed."
        ),
        page.digest,
    )


class ValueCheck:
    """One value that will be stored, and whether it traces to a verified quote."""

    def __init__(
        self,
        field: str,
        value: str,
        quote_check: QuoteCheck | None,
        in_quote: bool,
        detail: str,
    ) -> None:
        self.field = field
        self.value = value
        self.quote_check = quote_check
        self.in_quote = in_quote
        self.detail = detail

    @property
    def verified(self) -> bool:
        """Both stages passed: the quote is in the fetched bytes, the value is in the quote."""
        return (
            self.quote_check is not None and self.quote_check.verified and self.in_quote
        )


def literal_in_quote(value: str, quote: str) -> bool:
    """`value_in_quote` for text that is NOT a version: a URL or a chart name.

    `value_in_quote` switches to version-token matching the moment the value contains
    something version-shaped, and a chart repository URL may well (`.../v2/charts`). A
    URL is compared as text, case-insensitively, with whitespace collapsed and a trailing
    slash ignored -- `helm repo add` treats `https://x/` and `https://x` alike.

    >>> literal_in_quote("https://charts.jetstack.io/", "helm repo add jetstack https://charts.jetstack.io")
    True
    >>> literal_in_quote("keda", "helm install keda kedacore/keda")
    True
    >>> literal_in_quote("https://kedacore.github.io/charts", "helm repo add jetstack https://charts.jetstack.io")
    False
    """
    needle = _collapse_space(value).casefold().rstrip("/")
    haystack = _collapse_space(quote).casefold()
    return bool(needle) and needle in haystack


def _check_value(
    field: str,
    value: str,
    quote: str,
    url: str,
    revision: str,
    pages: PageSet,
    *,
    literal: bool = False,
) -> ValueCheck:
    quote_check = check_quote(quote, url, revision, pages)
    if not quote_check.verified:
        # The quote is unusable, so whether the value sits inside it says nothing. Left
        # false with the quote's own reason, rather than reporting a passing inner check
        # that rests on nothing.
        return ValueCheck(field, value, quote_check, False, quote_check.detail)
    present = literal_in_quote(value, quote) if literal else value_in_quote(value, quote)
    return ValueCheck(
        field,
        value,
        quote_check,
        present,
        (
            "traced to a verified quote in the cited page"
            if present
            else f"{value!r} does not appear in the verified quote for this value"
        ),
    )


def chart_values(
    extraction: ResearchExtraction,
) -> tuple[tuple[str, str, ChartProvenanceInput], ...]:
    """`(field, value, carrier)` for every chart value the lane would act on: the repository.

    Shared by the verifier, the judge and the approval report so all three enumerate the
    same thing. One value today; the shape is a tuple so a second one lands in one place.
    """
    if extraction.chart is None:
        return ()
    return (("chart_repository", extraction.chart.repository_url, extraction.chart),)


def _statement_values(
    s: CompatibilityStatementInput,
) -> tuple[tuple[str, str], ...]:
    """Every number a statement contributes to the stored fact, with a field name.

    This enumeration is the coverage fix. Each value listed here is one that ends up
    inside a `CompatibilityRule` and therefore inside a verdict, so each must trace to a
    quote -- storing them without checking any is the gap this closes.
    """
    values: list[tuple[str, str]] = []
    if s.stated_minimum is not None:
        values.append((f"stated_minimum[{s.version_spec}]", s.stated_minimum))
    if s.stated_maximum_inclusive is not None:
        values.append(
            (
                f"stated_maximum_inclusive[{s.version_spec}]",
                s.stated_maximum_inclusive,
            )
        )
    for minor in s.stated_minors:
        values.append((f"stated_minor[{s.version_spec}]", minor))
    for api in s.served_api_versions:
        values.append((f"served_api_version[{s.version_spec}]", api))
    return tuple(values)


class VerificationReport:
    """The whole verification result for one extraction: every quote, every stored value.

    `unverified_values` is what the acceptance gate reads. It is empty only when every
    value destined for the store traces to a quote that was found in bytes this run
    fetched.
    """

    def __init__(
        self,
        quote_checks: tuple[QuoteCheck, ...],
        value_checks: tuple[ValueCheck, ...],
        chart_checks: tuple[ValueCheck, ...] = (),
    ) -> None:
        self.quote_checks = quote_checks
        self.value_checks = value_checks
        # The chart repository's own check, outside the signals -- see `verify_extraction`.
        self.chart_checks = chart_checks

    @property
    def unverified_values(self) -> tuple[ValueCheck, ...]:
        return tuple(v for v in self.value_checks if not v.verified)

    @property
    def fabricated_quotes(self) -> tuple[QuoteCheck, ...]:
        """Quotes present in no fetched page. The splice detector's output."""
        return tuple(
            q
            for q in self.quote_checks
            if q.verdict in (QuoteCheck.ABSENT, QuoteCheck.NOT_FETCHED)
        )

    @property
    def misattributed_quotes(self) -> tuple[QuoteCheck, ...]:
        return tuple(q for q in self.quote_checks if q.verdict == QuoteCheck.WRONG_PAGE)

    @property
    def reattributed_quotes(self) -> tuple[QuoteCheck, ...]:
        """Quotes verified against a sibling query-page and re-cited to it.

        Verified, so they are NOT in `misattributed_quotes` -- but a reader (and the
        approval page) should see that the citation was moved, which is why they are
        enumerated separately rather than folded into the clean `PRESENT` set.
        """
        return tuple(
            q for q in self.quote_checks if q.verdict == QuoteCheck.VERIFIED_SIBLING_PAGE
        )

    def reattribution_for(
        self, quote: str, url: str, revision: str
    ) -> tuple[str, str] | None:
        """The `(url, revision)` a quote should be CITED to, if it was re-attributed.

        `None` when the quote was not re-attributed (cite it where the model did) or when
        no check matches. The assembler calls this so the citation it writes names the
        sibling page whose bytes carry the quote, not the stale page number the model
        wrote -- otherwise the re-attribution would show on the receipt but the stored
        fact would still cite the wrong page.
        """
        for q in self.quote_checks:
            if q.quote == quote and q.url == url and q.revision == revision:
                return (q.attributed_url, q.attributed_revision) if q.reattributed else None
        return None

    @property
    def verified_quotes(self) -> tuple[str, ...]:
        return tuple(q.quote for q in self.quote_checks if q.verified)

    @property
    def coverage(self) -> tuple[int, int]:
        """(verified, total) stored values -- the coverage number the fact is graded on."""
        return (
            sum(1 for v in self.value_checks if v.verified),
            len(self.value_checks),
        )

    def summary(self) -> str:
        verified, total = self.coverage
        return (
            f"{verified}/{total} stored value(s) trace to a quote verified against "
            f"fetched page bytes; {len(self.fabricated_quotes)} quote(s) appear in no "
            f"fetched page, {len(self.misattributed_quotes)} cite the wrong page, "
            f"{len(self.reattributed_quotes)} re-attributed to a sibling query-page."
        )


def verify_extraction(
    extraction: ResearchExtraction, pages: PageSet
) -> VerificationReport:
    """Check every quote against fetched bytes and every stored value against its quote.

    The enumeration covers claims, compatibility statements, releases and the recommended
    version -- everything that ends up in the stored fact. `UNSTATED` statements
    contribute no numbers, so they contribute no values to check.
    """
    quote_checks: list[QuoteCheck] = []
    value_checks: list[ValueCheck] = []

    def quote_once(quote: str, url: str, revision: str) -> None:
        if any(
            q.quote == quote and q.url == url and q.revision == revision
            for q in quote_checks
        ):
            return
        quote_checks.append(check_quote(quote, url, revision, pages))

    for claim in extraction.claims:
        quote_once(claim.quote, claim.source.url, claim.source.revision)
        value_checks.append(
            _check_value(
                claim.field,
                claim.value,
                claim.quote,
                claim.source.url,
                claim.source.revision,
                pages,
            )
        )

    for statement in extraction.statements:
        quote_once(statement.quote, statement.source.url, statement.source.revision)
        if statement.kind is BoundKind.UNSTATED:
            continue
        for field, value in _statement_values(statement):
            value_checks.append(
                _check_value(
                    field,
                    value,
                    statement.quote,
                    statement.source.url,
                    statement.source.revision,
                    pages,
                )
            )

    for release in extraction.releases:
        quote_once(release.quote, release.source.url, release.source.revision)
        value_checks.append(
            _check_value(
                "release",
                release.version,
                release.quote,
                release.source.url,
                release.source.revision,
                pages,
            )
        )

    # THE CHART IS CHECKED THE SAME WAY AND COUNTED SEPARATELY. A failed chart quote drops
    # the repository from the fact and shows red on the approval page; it does not touch the
    # signals that decide whether the COMPATIBILITY fact is accepted. A fact with 36 of 38
    # compatibility values verified must not be rejected because one chart quote had a line
    # missing: provenance and compatibility answer different questions, and one
    # must not veto the other.
    chart_checks = tuple(
        _check_value(
            field,
            value,
            carrier.quote,
            carrier.source.url,
            carrier.source.revision,
            pages,
            literal=True,  # a URL is text, not a version
        )
        for field, value, carrier in chart_values(extraction)
    )

    if extraction.recommended_version is not None:
        # The recommended version drives what an operator installs, so it is verified
        # like any other stored value -- through the release sighting that names it.
        sighting = next(
            (
                r
                for r in extraction.releases
                if r.version == extraction.recommended_version
            ),
            None,
        )
        if sighting is None:
            value_checks.append(
                ValueCheck(
                    "recommended_version",
                    extraction.recommended_version,
                    None,
                    False,
                    (
                        "the recommended version is not among the releases the model "
                        "quoted, so no quote supports installing it"
                    ),
                )
            )
        else:
            value_checks.append(
                _check_value(
                    "recommended_version",
                    extraction.recommended_version,
                    sighting.quote,
                    sighting.source.url,
                    sighting.source.revision,
                    pages,
                )
            )

    return VerificationReport(tuple(quote_checks), tuple(value_checks), chart_checks)
