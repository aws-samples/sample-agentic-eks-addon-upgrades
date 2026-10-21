"""Write the research agent's system prompt into the artifacts Terraform deploys.

**THE PROMPT LIVED IN TWO PLACES AND NOTHING HELD THEM TOGETHER.** `research/extract.py`
carries `SYSTEM_PROMPT` for the in-process path; `agents/readiness_research/harness.json` carries
`systemPrompt` for the deployed Harness (`infra/terraform/modules/research/harness.tf` reads that
key and
nothing else from the manifest). They were byte-identical by hand -- 40 KB of prose and a
pasted JSON schema -- and the day the contract grew a field, the pasted schema in one copy
would have stopped describing the object the other copy validates. The chart provenance
change was that day.

So Python is the source and the manifest is an artifact of it, exactly as `tools/brief/generate.py`
arranged for the brief. `SYSTEM_PROMPT` itself now renders its schema from the same generator the
contract validation uses, so the prose, the schema and the model are one edit.

    python -m tools.research.generate            # write the files
    python -m tools.research.generate --check     # fail if they are stale

**ONLY `systemPrompt` IS OWNED HERE.** The manifest's other keys -- model, tools, timeouts, the
gateway ARN -- are read back and written unchanged, because they are configured by Terraform
variables and this module has no opinion on them. `system-prompt.md` beside it is the same bytes
as a reviewable file, which is what a diff on a prompt change should show.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eks_addon_readiness.research.extract import SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "agents" / "readiness_research"
MANIFEST = APP / "harness.json"
PROMPT = APP / "system-prompt.md"


def manifest_text() -> str:
    """The manifest as committed, with `systemPrompt` replaced by the Python constant."""
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["systemPrompt"] = SYSTEM_PROMPT
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def artifacts() -> dict[Path, str]:
    return {MANIFEST: manifest_text(), PROMPT: SYSTEM_PROMPT}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any generated file is stale, writing nothing",
    )
    args = parser.parse_args(argv)

    stale: list[str] = []
    for path, text in artifacts().items():
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == text:
            continue
        if args.check:
            stale.append(str(path.relative_to(ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")

    if stale:
        print(
            "STALE, so the deployed prompt would not be the reviewed one:\n  "
            + "\n  ".join(stale)
            + "\n\nRun `uv run python -m tools.research.generate` and commit the result.",
            file=sys.stderr,
        )
        return 1
    if args.check:
        print("research prompt artifacts are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
