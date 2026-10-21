"""Every `{{ Parameter }}` in the release gate document must be legal where it is written.

WHY THIS FILE EXISTS, AND IT IS THE ONLY HONEST REASON: AWS caught a defect that 402 green
tests did not, at apply time, having already created ten of the component's thirteen resources.
The message was

    Parameter "Changes" is "STRING_LIST" type and can't be used as a substring parameter

because `Changes` was declared `StringList` and then interpolated into the middle of two mail
bodies. SSM substitutes a `StringList` only where a list is consumed AS a list -- the whole
value of an input -- and refuses it inside a string. Nothing local renders an SSM document, so
nothing local could refuse it: `terraform validate` type-checks HCL, `yamlencode` produces
valid YAML whatever the words mean, and no unit test in this repository had an opinion about
the document's own grammar. That is the gap this file closes, and it closes it for the CLASS
rather than for `Changes`: every reference, every declared type, both directions.

THE FOUR FAILURES THIS WOULD HAVE CAUGHT, all of which are apply-time or run-time in AWS and
none of which any other test sees:

    1. A `StringList` used as a substring         -- the defect above: document creation fails.
    2. A reference to a parameter that is not     -- SSM refuses the document, or worse renders
       declared, or a misspelled pseudo-parameter    the literal `{{ Foo }}` into a person's mail.
    3. A declared parameter nothing references    -- every start call must supply a value that
                                                     is then read by nothing.
    4. A `States.Array` wrapper that disagrees    -- the SSM API's `Parameters` map is
       with the declared type in workflow.tf         name -> LIST of strings. A `String`
                                                     parameter passed unwrapped, or a
                                                     `StringList` wrapped, is refused at
                                                     `StartAutomationExecution` -- which lands
                                                     on `Release rejected`, so the symptom is a
                                                     refused release with the cause two states
                                                     away.

THE HCL IS THE SOURCE, ON PURPOSE. The alternative is calling `terraform` here to render the
document, which would make this test need a binary, a provider lockfile and a plan -- so it
would be skipped in exactly the circumstances it matters. The document body is a literal
`yamlencode({...})` expression with no conditionals in it, so reading the HCL reads the document.

COMMENT LINES ARE STRIPPED FIRST, and that is not a detail: the comments in `release_gate.tf`
QUOTE `{{ Changes }}` and `{{ Summary }}` while explaining them, and `yamlencode` never sees a
comment. A version of this test that skipped that step would report references that do not
exist in the deployed document -- and, worse, would pass when a reference was deleted from the
body but left in the prose.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_GATE = ROOT / "infra/terraform/modules/platform/release_gate.tf"
WORKFLOW = ROOT / "infra/terraform/modules/platform/workflow.tf"

# `{{ Name }}` and `{{ Name:SUBFIELD }}`. SSM tolerates inner spaces; so does this.
REFERENCE = re.compile(r"\{\{ *([A-Za-z0-9_:]+) *\}\}")

# The pseudo-parameters SSM itself substitutes. Not declared anywhere and must not be: a
# document that declared `global` would be shadowing a service-supplied value.
PSEUDO_PREFIXES = frozenset({"global", "automation", "ssm", "aws"})

# A parameter reference is a LIST substitution only when it is the entire value of an input.
WHOLE_VALUE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_]* = \"\{\{ *[A-Za-z0-9_:]+ *\}\}\"\s*$")


def _document() -> str:
    """The `content = yamlencode({ ... })` body, with whole-line `#` comments removed."""
    text = RELEASE_GATE.read_text()
    start = text.index("content = yamlencode({")
    end = text.index("\n  })\n", start)
    body = text[start:end]
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def _declared_types() -> dict[str, str]:
    """name -> declared SSM type, read from the document's `parameters` block."""
    doc = _document()
    block = re.search(r"\n    parameters = \{\n(.*?)\n    \}\n", doc, re.S)
    assert block, "the release gate document declares no parameters block"
    found = {
        m.group(1): m.group(2)
        for m in re.finditer(
            r"^      ([A-Za-z][A-Za-z0-9_]*) = \{\n.*?^        type        = \"(\w+)\"",
            block.group(1),
            re.S | re.M,
        )
    }
    assert found, "no parameter declarations parsed out of the document"
    return found


def _references() -> list[tuple[str, bool, str]]:
    """Every reference in the document body: (name, is_whole_value, the line it is on).

    The `parameters` block itself is excluded -- a parameter's own declaration mentions no
    reference, but its DESCRIPTION legitimately could, and a description is not a substitution.
    """
    doc = _document()
    block = re.search(r"\n    parameters = \{\n.*?\n    \}\n", doc, re.S)
    body = doc.replace(block.group(0), "\n") if block else doc

    seen: list[tuple[str, bool, str]] = []
    for line in body.splitlines():
        names = REFERENCE.findall(line)
        if not names:
            continue
        whole = bool(WHOLE_VALUE.match(line)) and len(names) == 1
        seen.extend((name, whole, line.strip()) for name in names)
    return seen


def test_every_reference_names_something_that_exists() -> None:
    """A reference to an undeclared parameter, or a misspelled pseudo-parameter.

    THE PSEUDO-PARAMETER HALF IS THE QUIETER FAILURE. `{{ global:REGION }}` is substituted by
    SSM; `{{ globals:REGION }}` is not a document error at all -- it is delivered verbatim in
    the mail, inside the console URL the approver is asked to click.
    """
    declared = _declared_types()
    for name, _whole, line in _references():
        if ":" in name:
            prefix = name.split(":", 1)[0]
            assert prefix in PSEUDO_PREFIXES, (
                f"{{{{ {name} }}}} names the pseudo-parameter namespace {prefix!r}, which SSM "
                f"does not substitute, so the literal text is delivered to a reader: {line}"
            )
            continue
        assert name in declared, (
            f"{{{{ {name} }}}} is referenced but not declared in the document's parameters: "
            f"{line}. SSM refuses a document that substitutes an undeclared parameter."
        )


def test_no_string_list_parameter_is_used_inside_a_string() -> None:
    """THE DEFECT ITSELF. A `StringList` is legal only as the whole value of an input.

    `Parameter "X" is "STRING_LIST" type and can't be used as a substring parameter` is an
    apply-time failure, which means it is a failure with ten resources already created -- the
    shape of failure this repository is otherwise careful to keep at plan time.
    """
    declared = _declared_types()
    for name, whole, line in _references():
        if declared.get(name) != "StringList":
            continue
        assert whole, (
            f"{{{{ {name} }}}} is declared StringList and is used inside a string: {line}\n"
            "SSM substitutes a StringList only where the reference is the ENTIRE value of an "
            "input. Either the parameter is a String and the list is joined before it is "
            "passed, or the reference moves to a whole-value position -- and for a mail body "
            "only the first is possible."
        )


def test_the_approvers_input_is_a_list_and_is_passed_bare() -> None:
    """The one place a `StringList` is REQUIRED, asserted from the other side.

    `aws:approve`'s `Approvers` consumes a list. A `String` there is one comma-joined
    pseudo-ARN matching no principal, so the automation waits out its timeout and the run fails
    having asked nobody -- no error, no log, nothing naming the cause. That failure mode is why
    fixing the `Changes` defect by making both parameters `String` would be wrong.
    """
    declared = _declared_types()
    assert declared.get("Approvers") == "StringList", (
        f"Approvers is declared {declared.get('Approvers')!r}. aws:approve consumes a list; a "
        "String arrives as one comma-joined pseudo-ARN that matches nobody and pauses forever."
    )
    refs = [(whole, line) for name, whole, line in _references() if name == "Approvers"]
    assert refs, "nothing in the document substitutes Approvers"
    for whole, line in refs:
        assert whole, f"Approvers must be passed bare as a whole value, not within: {line}"


def test_every_declared_parameter_is_substituted_somewhere() -> None:
    """A declared parameter nothing reads is a value every start call must supply for nothing.

    It is not an AWS error, which is why it needs a test: the document creates, the run works,
    and `workflow.tf` keeps passing a value that reaches no step -- so a reader of either file
    believes the parameter is doing something.
    """
    referenced = {name for name, _whole, _line in _references()}
    for name in _declared_types():
        assert name in referenced, (
            f"the document declares {name} and substitutes it nowhere. It has no default, so "
            "every start call must supply a value that nothing then reads."
        )


def test_the_start_call_passes_exactly_the_declared_parameters() -> None:
    """`workflow.tf`'s `Parameters` map and the document's declarations must be the same set.

    A missing one fails the start call for a parameter with no default; an extra one is refused
    outright as an unknown parameter. Both land on `Release rejected`, which is the correct
    outcome and a uselessly vague one -- the run reports that nobody could be asked, and the
    reason is in a state two steps back.
    """
    block = re.search(
        r'"Release gate" = \{.*?\n        Parameters = \{\n(.*?)\n        \}\n',
        WORKFLOW.read_text(),
        re.S,
    )
    assert block, 'no Parameters map found on the "Release gate" state in workflow.tf'
    passed = {
        m.group(1)
        for m in re.finditer(
            r'^\s*"([A-Za-z][A-Za-z0-9_]*)\.\$"\s*=', block.group(1), re.M
        )
    }
    declared = set(_declared_types())
    assert passed == declared, (
        f"the start call passes {sorted(passed)} and the document declares {sorted(declared)}. "
        f"Only in the start call: {sorted(passed - declared)}. Only in the document: "
        f"{sorted(declared - passed)}."
    )


def test_the_start_call_wraps_strings_and_never_wraps_lists() -> None:
    """The SSM `Parameters` map is name -> LIST of strings, so the two types are passed differently.

    A `String` parameter must be wrapped -- `States.Array(...)` -- to become a one-element list.
    A `StringList` parameter is already a list of strings and must NOT be wrapped, or SSM gets a
    list containing a list. THIS IS THE SAME DEFECT AS THE SUBSTRING ONE SEEN FROM THE OTHER
    END: changing a parameter's declared type without changing how it is passed swaps one
    apply-time failure for a run-time one.
    """
    block = re.search(
        r'"Release gate" = \{.*?\n        Parameters = \{\n(.*?)\n        \}\n',
        WORKFLOW.read_text(),
        re.S,
    )
    assert block, 'no Parameters map found on the "Release gate" state in workflow.tf'
    declared = _declared_types()
    for match in re.finditer(
        r'^\s*"([A-Za-z][A-Za-z0-9_]*)\.\$"\s*=\s*"(.*)"\s*$', block.group(1), re.M
    ):
        name, value = match.group(1), match.group(2)
        wrapped = value.startswith("States.Array(")
        if declared.get(name) == "StringList":
            assert not wrapped, (
                f"{name} is a StringList and is wrapped in States.Array: {value}. It is "
                "already a list of strings; wrapping yields a list containing a list, which "
                "SSM refuses at StartAutomationExecution."
            )
        else:
            assert wrapped, (
                f"{name} is a {declared.get(name)} and is passed unwrapped: {value}. The SSM "
                "Parameters map takes name -> list of strings, so a single string must become "
                "a one-element list via States.Array."
            )
