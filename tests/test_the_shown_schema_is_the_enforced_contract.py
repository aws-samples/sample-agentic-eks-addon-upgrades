"""The schema the agent is shown is generated from the contract its answer is validated against.

WHAT THIS CATCHES. The prompt used to carry a hand-written JSON example while the reply was checked
against `UpgradeTriage`, which the prompt never saw. Two descriptions of one contract, in two files,
with nothing holding them together. They agreed on the day they were written, and every way they
could stop agreeing is silent:

  a field added to `UpgradeTriage`        the model is shown the old shape, answers correctly by the
                                         prompt, and validation refuses it
  a verdict renamed in `TriageVerdict`    the model is shown a value that no longer exists and its
                                         answer cannot validate, on every component, every run
  a field the model must not set          shown as settable, filled in, then silently overwritten by
                                         the proxy -- the answer looks honoured and is not

None of those surface as "the prompt and the contract disagree", which is the only description that
would send somebody to the right file. `brief/prompt.py::extraction_schema` already solved this for
the brief agent; this asserts the upgrade agent's version of it.

IT IS NOT MODEL-LEVEL ENFORCEMENT AND THIS FILE DOES NOT CLAIM IT IS. `research/extract.py` passes
its schema as `output_config.format`, which constrains generation. That parameter returns 400 on the
Harness endpoint -- both `brief_harness.py` and `research_harness.py` record having tried -- so the
schema here reaches the model as text. What that removes is a drift path, not the possibility of a
malformed answer, which is why `model_validate` and the re-ask stay and are asserted below.

Deliberately no AWS call: the schema is a pure function of the contract.
"""

from __future__ import annotations

import json

from eks_addon_readiness.upgrade_lane.models import (
    TriageAction,
    TriageVerdict,
    UpgradeTriage,
)
from eks_addon_readiness.upgrade_lane.prompt import SYSTEM_PROMPT, triage_schema

# The proxy stamps these on after parsing. A model shown them would supply values that are then
# discarded, and `schema_version`/`kind` are `Literal` constants it could only get wrong.
PROXY_OWNED = ("checkpoint", "elapsed_seconds", "schema_version", "kind")


def test_the_schema_is_generated_from_the_contract_not_written_beside_it() -> None:
    """Every property shown must exist on the model, and every enum must match its members.

    This is the assertion that makes the other failures impossible rather than merely unlikely: if
    the schema were hand-written, nothing here would fail when the contract moved.
    """
    shown = triage_schema()
    contract = UpgradeTriage.model_json_schema()

    for name in shown["properties"]:
        assert name in contract["properties"], (
            f"the shown schema offers {name!r}, which UpgradeTriage has no field for. A model "
            "supplying it has its answer refused by `extra=forbid` after being told to send it."
        )

    verdicts = sorted(shown["$defs"]["TriageVerdict"]["enum"])
    actions = sorted(shown["$defs"]["TriageAction"]["enum"])
    assert verdicts == sorted(v.value for v in TriageVerdict)
    assert actions == sorted(a.value for a in TriageAction)


def test_the_schema_requires_what_the_validators_require() -> None:
    """`observations` is required and non-empty, because the model validator refuses it empty.

    A schema that left it optional would permit precisely the answer the contract rejects -- a
    verdict citing nothing it read -- which is the shown rule and the enforced rule disagreeing in
    the direction that wastes a checkpoint.
    """
    shown = triage_schema()
    assert "observations" in shown["required"], (
        "`observations` is not required by the shown schema, but "
        "`UpgradeTriage.every_verdict_must_say_what_it_read` refuses every verdict without it."
    )
    assert shown["properties"]["observations"].get("minItems") == 1, (
        "`observations` has no minItems, so `[]` satisfies the schema and is then refused by the "
        "contract. Required-but-empty is the same failure as absent."
    )
    assert shown["additionalProperties"] is False, (
        "the shown schema permits extra properties while the contract forbids them, so a model can "
        "be invited to add a field whose answer is then refused."
    )


def test_the_proxy_owned_fields_are_not_offered_to_the_model() -> None:
    """Shown-and-overwritten is worse than absent: the answer looks honoured and is not."""
    shown = triage_schema()
    for field in PROXY_OWNED:
        assert field not in shown["properties"], (
            f"{field!r} is shown to the model. The proxy sets it from the build's clock or it is a "
            "Literal constant, so a value the model supplies is discarded without complaint."
        )


def test_the_system_prompt_carries_the_generated_schema_and_no_placeholder() -> None:
    """The substitution happens, and it happens with the generated text rather than a copy."""
    assert "__TRIAGE_SCHEMA__" not in SYSTEM_PROMPT, (
        "the schema placeholder was never substituted, so the agent is told to match a literal "
        "`__TRIAGE_SCHEMA__`."
    )
    rendered = json.dumps(triage_schema(), indent=2, sort_keys=True)
    assert rendered in SYSTEM_PROMPT, (
        "the system prompt does not contain the generated schema verbatim. If it carries a "
        "reformatted or hand-edited copy instead, the drift this file exists to prevent is back."
    )


def test_the_validation_and_re_ask_are_still_there() -> None:
    """The schema is text on this endpoint, so the checks that are not text must remain.

    `output_config.format` returns 400 on the Harness API, so nothing constrains generation. If the
    proxy stopped validating, a malformed answer would reach the lane's decision table with only a
    prompt behind it.
    """
    from pathlib import Path

    proxy = (
        Path(__file__).resolve().parent.parent
        / "eks_addon_readiness/stages/upgrade_triage.py"
    ).read_text()
    assert "UpgradeTriage.model_validate" in proxy, (
        "the proxy no longer validates the answer against the contract. On this endpoint the "
        "schema in the prompt is advice; this is the enforcement."
    )
    assert "REASK" in proxy, (
        "the proxy no longer re-asks on a validation failure, so one malformed answer costs the "
        "whole checkpoint."
    )
