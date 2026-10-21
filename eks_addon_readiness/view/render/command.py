"""The one signed command an approver runs. A renderer, and fingerprinted as one.

WHY THIS IS ITS OWN MODULE, AND WHY IT IS UNDER `render/`. It began in `view/model.py`,
which is in `RULESET_MODULES` -- so editing the command moved `present_ruleset_digest`
and left `present_renderer_digest` untouched. That is precisely inverted. The two
digests exist so a reader finding two differing reports can tell whether the NUMBERS
changed or only the BYTES did, and this module produces bytes: it decides nothing, it
counts nothing, and every value it prints was handed to it. A stylesheet tweak and a
command tweak are the same kind of edit and must move the same fingerprint.

It is a module rather than a function inside `html.py` because the per-candidate
evidence receipt (`research/render_html.py`) renders the same control. Two
hand-written copies of a signed request drift the moment either page changes, and a
reviewer who found the two pages spelling the request differently would have no way to
tell which one the endpoint actually accepts. So there is one spelling, and a test
asserts both pages emit it byte-identically.

Imports the standard library and nothing else -- not even `..model`. It needs no
contract: it takes strings and returns a string.

`region_of` deliberately lives in `build.py` and not here, even though it exists only to
feed this module: it derives a value that is STORED on the presentation document, so it
belongs to the ruleset fingerprint. A renderer computing a stored value is the inversion
this package exists to prevent.
"""

from __future__ import annotations

# The placeholder a human MUST replace before running a command. Upper-case and
# hyphenated so an unedited command is obvious in a terminal, in a screenshot and on a
# projector. NOT merely a hint: `research/approval.py` refuses a decision whose comment
# or reason still contains one, because a placeholder stored as the audit record is
# worse than an empty one -- it reads like a considered answer.
REPLACE_COMMENT = "REPLACE-WITH-WHAT-YOU-CHECKED"
REPLACE_REASON = "REPLACE-WITH-WHY-YOU-ARE-REJECTING"


def decision_command(
    *,
    endpoint: str,
    region: str,
    action: str,
    component_id: str,
    version_series: str,
    candidate_uri: str,
    candidate_digest: str,
    report_digest: str,
) -> str:
    """The command that approves or refuses one proposed fact.

    WHY A COMMAND AND NOT A BUTTON. The endpoint is a Lambda function URL with
    `AWS_IAM` auth, so AWS resolves the caller from a SigV4 signature before the
    handler runs. A page served from S3 holds no credential and can sign nothing: a
    plain form POST arrives unsigned and is refused by the authorizer with a bare 403
    that explains none of this. Presigning at render time would work mechanically and
    is worse, because the signature would then sit inside an object every reader of the
    report can fetch -- including any agent that renders report links. That would hand
    the agent a working approval, which is the single power the whole design denies it.

    So the signature is the human's. `curl --aws-sigv4` reads their credentials from
    the environment and signs locally, including the payload hash, which is why the body
    is spelled out here in full: the request AWS authorises is byte-for-byte the request
    the page displayed.

    THE FIRST LINE IS A CREDENTIAL GUARD, and it is not decoration. With no credentials
    exported -- a profile or an SSO session, which is the normal state of a laptop --
    `$AWS_ACCESS_KEY_ID` expands to empty, curl signs with an empty key, and the endpoint
    answers with exactly the same silent 403 the old browser form did. `${VAR:?}` makes
    the shell refuse before curl runs and name the missing variable, so the failure is
    local, immediate and legible.

    It is ONE preflight line rather than `:?` inside each `--user` and header expansion,
    which is what this did first. Three copies of a helpful message made the block three
    lines longer than the request itself and buried the thing a reader is trying to
    read -- on a projector the command became unreadable. The guard belongs where it can
    be terse; the advice belongs in the page's prose.

    FOUR FIELDS IDENTIFY THE FACT, not three. The URI and the two digests are what the
    endpoint verifies; `component_id` and `version_series` are cross-checked against the
    loaded candidate so that the fact NAMED on the page is provably the fact approved. A
    page whose prose said "keda" beside another component's digests would otherwise let
    a reader approve something they never read about.

    >>> print(decision_command(endpoint="https://x.lambda-url.us-east-1.on.aws/",
    ...     region="us-east-1", action="reject", component_id="keda",
    ...     version_series="2.17.x", candidate_uri="s3://b/c.json",
    ...     candidate_digest="sha256:aa", report_digest="sha256:bb").splitlines()[4])
      --aws-sigv4 'aws:amz:us-east-1:lambda' \\
    """
    field = "comment" if action == "approve" else "reason"
    note = REPLACE_COMMENT if action == "approve" else REPLACE_REASON
    body = (
        "{"
        f'"action":"{action}",'
        f'"component_id":"{component_id}",'
        f'"version_series":"{version_series}",'
        f'"candidate_uri":"{candidate_uri}",'
        f'"candidate_digest":"{candidate_digest}",'
        f'"report_digest":"{report_digest}",'
        f'"{field}":"{note}"'
        "}"
    )
    return _signed_post(endpoint, region, body)


def done_command(*, endpoint: str, region: str, run_id: str) -> str:
    """The command that says "I have finished deciding", and lets the paused run continue.

    WHY THERE IS A THIRD COMMAND AT ALL. Approve and reject each settle ONE proposal. The
    run is waiting on a person, and nothing in an individual decision says whether that
    person is finished -- they may intend to approve two of three and leave the third, or
    to come back after reading a changelog. A count cannot answer it either: "all three
    decided" is a guess at intent, and guessing wrong resumes a pipeline while somebody is
    still reading. So finishing is its own explicit act.

    WHY IT IS A COMMAND AND NOT A BUTTON, for the same reason as the other two and worth
    restating because this one *feels* like a button: the endpoint is an `AWS_IAM` function
    URL, a page in S3 holds no credential, and an unsigned POST is refused by the
    authorizer with a bare 403. A real `<button>` here would be a control that silently
    fails.

    IT CARRIES THE RUN, NOT A CANDIDATE. The task token belongs to the execution, and one
    execution resumes once however many proposals it queued -- so the run id is the whole
    identifier. The token itself is deliberately NOT in this command: it is read from the
    token store by the endpoint, so a page anyone can fetch never contains the thing that
    resumes the pipeline.

    >>> print(done_command(endpoint="https://x.lambda-url.us-east-1.on.aws/",
    ...     region="us-east-1", run_id="abc").splitlines()[-1])
      -d '{"action":"done","run_id":"abc"}'
    """
    body = f'{{"action":"done","run_id":"{run_id}"}}'
    return _signed_post(endpoint, region, body)


def _signed_post(endpoint: str, region: str, body: str) -> str:
    """One spelling of the signed request, shared by every decision command.

    Extracted when `done` became a third action rather than left duplicated: a reviewer
    who found two commands spelling the same request differently would have no way to tell
    which one the endpoint accepts, which is the argument this module's docstring already
    makes for existing at all.

    THE FIRST LINE IS A CREDENTIAL GUARD, and it is not decoration. With no credentials
    exported -- a profile or an SSO session, which is the normal state of a laptop --
    `$AWS_ACCESS_KEY_ID` expands to empty, curl signs with an empty key, and the endpoint
    answers with exactly the same silent 403 the old browser form did. `${VAR:?}` makes the
    shell refuse before curl runs and name the missing variable.
    """
    return (
        '# fails here, not with a 403, if your credentials are not exported:\n'
        '#   eval "$(aws configure export-credentials --format env)"\n'
        ': "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}" "${AWS_SESSION_TOKEN:?}"\n'
        f"curl -sS --fail-with-body -X POST '{endpoint}' \\\n"
        f"  --aws-sigv4 'aws:amz:{region}:lambda' \\\n"
        '  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \\\n'
        '  -H "x-amz-security-token: $AWS_SESSION_TOKEN" \\\n'
        "  -H 'Content-Type: application/json' \\\n"
        f"  -d '{body}'"
    )
