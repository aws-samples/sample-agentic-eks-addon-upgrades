#!/usr/bin/env bash
# Build the Lambda deployment packages.
#
# One zip per stage. Each zip has the package at its root.
#
# CRITICAL: sources are shipped as .py, never precompiled and never stripped. The
# ruleset digests are computed at runtime from each rule module's own source text,
# so a package carrying only bytecode would make every stage raise
# RulesetUnavailable. That is why there is no --no-compile / pyc-only step here,
# and why removing .py files to save space would break the build in a way no unit
# test would catch.
#
# Deterministic: files are added in sorted order with a fixed timestamp, so the
# same sources produce the same zip bytes and Terraform does not redeploy on noise.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/build"
# PYTHON MUST BE ABSOLUTE IF IT IS SET AT ALL. The digest probe below runs from `/tmp` so that it
# imports the extracted zip and not the source tree, which means a relative override resolves against
# `/tmp` and dies with "No such file or directory" AFTER every zip has already been built. Resolved
# here rather than trusted, so a relative path works instead of failing two minutes in.
PY="${PYTHON:-python3}"
if [[ "${PY}" == */* && "${PY}" != /* ]]; then
  PY="$(cd "$(dirname "${PY}")" && pwd)/$(basename "${PY}")"
fi

# Wheels are fetched with uv when it is available and pip otherwise. Both resolve
# the same pinned versions; uv is just faster and does not require pip to be
# present in the interpreter being used.
if command -v uv >/dev/null 2>&1; then
  install_wheels() { uv pip install --quiet --target "$@"; }
else
  install_wheels() { "${PY}" -m pip install --quiet --target "$@"; }
fi

rm -rf "${BUILD}"
mkdir -p "${BUILD}"

# ---------------------------------------------------------------------------
# Shared layer content: the package and pydantic.
# boto3 is provided by the Lambda runtime and is deliberately NOT bundled.
# ---------------------------------------------------------------------------
# The Harness proxy is the ONE package that bundles boto3, and it has to. `InvokeHarness` is
# a recent API and the Lambda runtime's bundled boto3 does not carry it -- a proxy relying on
# the runtime copy fails at call time with "object has no attribute 'invoke_harness'", which
# is a deployment failure that no local test sees. Pinned to the boto3 version in uv.lock,
# which has the operation (it first appeared in 1.43.83). No pydantic and no model client:
# this package invokes two AWS APIs and parses JSON.
harness_proxy_deps() {
  install_wheels "$1" \
    --only-binary=:all: --python-platform aarch64-manylinux2014 \
    --python-version 3.12 \
    'boto3==1.43.93' 'pydantic==2.13.4'
}

stage_deps() {
  install_wheels "$1" \
    --only-binary=:all: --python-platform aarch64-manylinux2014 \
    --python-version 3.12 \
    'pydantic==2.13.4'
}

scan_deps() {
  install_wheels "$1" \
    --only-binary=:all: --python-platform aarch64-manylinux2014 \
    --python-version 3.12 \
    'pydantic==2.13.4' 'mcp-proxy-for-aws==1.6.4' 'strands-agents==1.54.0'
}

# Stage 6 only. The model client, and no transport: research reads public HTTPS
# sources with the standard library, so this package carries no MCP client and no
# agent framework. The freshness sweep shares the zip and calls no model at all --
# it compares a stored timestamp against a clock -- but sharing one artifact keeps
# the two handlers from drifting apart, exactly as stages 2-5 share theirs.
#
# The SDK version is a FLOOR, not a preference. Structured outputs need
# `messages.parse()` and the `output_config` parameter, and an older SDK fails in
# deployment with
#
#   TypeError: Messages.create() got an unexpected keyword argument 'output_config'
#
# which is how this pin was found -- a live invocation, not a local import, because
# the local checks drive the loop with a double. Do not lower it.
research_deps() {
  install_wheels "$1" \
    --only-binary=:all: --python-platform aarch64-manylinux2014 \
    --python-version 3.12 \
    'pydantic==2.13.4' "anthropic==${ANTHROPIC_SDK_VERSION:-1.5.0}"
}

assemble() {
  local name="$1" entry="$2" deps_fn="$3"
  local dir="${BUILD}/${name}"
  echo "==> assembling ${name}"
  mkdir -p "${dir}"
  cp -R "${ROOT}/eks_addon_readiness" "${dir}/"
  # THE ENTRY MODULE GOES IN `entrypoints/`, AND ONLY THIS ZIP'S ONE. Terraform names handlers
  # `entrypoints.<module>.<fn>`, so the package (its `__init__.py`) and this one module are
  # copied; the other entry modules stay out, which is what keeps the vendored boto3 of the
  # harness zips from being importable -- or needed -- anywhere else.
  mkdir -p "${dir}/entrypoints"
  cp    "${ROOT}/entrypoints/__init__.py" "${dir}/entrypoints/"
  cp    "${ROOT}/${entry}"            "${dir}/entrypoints/"
  "${deps_fn}" "${dir}"
  # Drop caches and bytecode. .py sources stay -- see the header.
  find "${dir}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "${dir}" -name '*.pyc' -delete 2>/dev/null || true
  #
  # *.dist-info IS KEPT, deliberately. An earlier version of this script deleted it
  # to shrink the zip, and Stage 1 then failed in Lambda with
  #
  #   PackageNotFoundError: No package metadata was found for mcp-proxy-for-aws
  #
  # because mcp_proxy_for_aws/__init__.py:21 calls
  # importlib.metadata.version('mcp-proxy-for-aws') at import time, which reads
  # dist-info. Any dependency may do this, so stripping installed metadata is not
  # a safe size optimisation in general -- and the failure appears only in a real
  # deployment, never in a local import.
  # THE FIXED TIMESTAMP the header promises. `cp` and the wheel installer stamp every file with
  # the build time and zip records it, so without this every build has new bytes and Terraform
  # redeploys all Lambdas on every plan. 1980-01-01 is the earliest time a zip entry can hold.
  find "${dir}" -exec touch -h -t 198001010000 {} +
  ( cd "${dir}" && find . -type f | sort | zip -q -X -@ "${BUILD}/${name}.zip" )
  echo "    ${BUILD}/${name}.zip  $(du -h "${BUILD}/${name}.zip" | cut -f1)"
}

# TWO stage artifacts, not one, and the split is the point.
#
#   stage1.zip   Stage 1 only. Carries the EKS MCP transport, because reading a
#                private-endpoint cluster needs it.
#   stages.zip   Stages 2-5. pydantic and nothing else.
#
# Stages 2-5 are pure functions of documents, so their package contains no
# transport at all -- which makes "Stage 4 cannot read a cluster" a property of the
# artifact rather than a claim about the code. It is also much smaller: the MCP and
# agent dependencies dominate the zip.
assemble stage1    entrypoints/stages.py               scan_deps
assemble stages    entrypoints/stages.py               stage_deps
assemble research  entrypoints/stages.py               research_deps
assemble research_harness entrypoints/research_harness.py harness_proxy_deps

# The upgrade brief. ITS OWN ZIP for the same reason research_harness has one: the proxy calls
# `InvokeHarness`, which is newer than the Lambda runtime's bundled boto3, so it needs the
# vendored copy `harness_proxy_deps` installs. The zip carries all four brief handlers -- the two
# orchestration ends, the per-component proxy and the search_issues tool -- because they share a
# package, not because they share permissions: infra/terraform/modules/brief gives each its own role.
#
# THE MANIFEST IS CHECKED BEFORE IT IS PACKAGED. `brief/prompt.py` is the single source of the
# agent's instruction and `agents/readiness_brief/harness.json` is generated from it; a stale
# manifest would deploy a prompt nobody reviewed, which is the one failure this arrangement could
# introduce. Fail the build here rather than let the apply succeed with the wrong instruction.
echo
echo "==> checking the brief agent's manifest and tool schemas are current"
# RUN UNDER THE PROJECT'S INTERPRETER, NOT THE PATH'S. `brief_tools.generate` imports the
# package, which needs `enum.StrEnum` -- Python 3.11+. `${PY}` honours the PYTHON override but
# defaults to `python3`, which on a stock macOS is CommandLineTools 3.9, and the check then fails
# with `ImportError: cannot import name 'StrEnum'` -- a message that names the interpreter and not
# the mistake. Measured: that is exactly how this step failed the first time it ran.
#
# `uv run` uses the interpreter `pyproject.toml` pins (>=3.12) with no dependence on PATH, and uv
# is already required to run this repository's tests. The fallback keeps a uv-less machine working
# rather than making this step the first thing to break a build for a lane that never touches the
# brief.
if command -v uv >/dev/null 2>&1; then
  BRIEF_PY=(uv run python)
else
  BRIEF_PY=("${PY}")
fi
( cd "${ROOT}" && "${BRIEF_PY[@]}" -m tools.brief.generate --check ) \
  || { echo "FAIL: regenerate with 'uv run python -m tools.brief.generate' and commit the result"; exit 1; }

assemble brief_harness entrypoints/brief_harness.py harness_proxy_deps

echo "==> checking the research agent's system prompt is current"
# `infra/terraform/modules/research/harness.tf` deploys `harness.json`'s `systemPrompt`; `extract.py` is what
# the repository reviews and what the in-process path runs. `tools/research/generate.py` writes
# the one from the other, and this check refuses a build where they differ.
( cd "${ROOT}" && "${BRIEF_PY[@]}" -m tools.research.generate --check ) \
  || { echo "FAIL: regenerate with 'uv run python -m tools.research.generate' and commit the result"; exit 1; }

echo "==> checking the upgrade agent's system prompt is current"
# THE DEPLOYED INSTRUCTION AND THE REVIEWED ONE MUST BE THE SAME BYTES. `infra/terraform/modules/upgrade` reads
# `agents/readiness_upgrade/system-prompt.md` with `file()`, and the text lives in
# `upgrade_lane/prompt.py::SYSTEM_PROMPT` so it sits beside the contract it describes and moves the
# prompt ruleset digest when it changes. A stale file would deploy an instruction nobody reviewed,
# and `tests/test_the_upgrade_agent_holds_no_write_path.py` asserts the same equality in the suite --
# this check is here so a build cannot produce a package whose prompt disagrees with its code.
( cd "${ROOT}" && "${BRIEF_PY[@]}" -c "
import sys, pathlib
from eks_addon_readiness.upgrade_lane.prompt import SYSTEM_PROMPT
path = pathlib.Path('agents/readiness_upgrade/system-prompt.md')
if not path.is_file() or path.read_text() != SYSTEM_PROMPT:
    sys.exit(1)
" ) || { echo "FAIL: agents/readiness_upgrade/system-prompt.md is stale. Regenerate it from upgrade_lane/prompt.py::SYSTEM_PROMPT and commit the result"; exit 1; }
echo "    ok - the deployed prompt and prompt.py agree"

assemble upgrade_harness entrypoints/upgrade_harness.py harness_proxy_deps

echo
echo "==> package sizes (Lambda: 50MB zipped, 250MB unzipped)"
for z in stage1 stages research research_harness brief_harness upgrade_harness; do
  printf '    %-10s zipped %-7s unzipped %s\n' "${z}" \
    "$(du -h "${BUILD}/${z}.zip" | cut -f1)" \
    "$(du -sh "${BUILD}/${z}" | cut -f1)"
done

echo
echo "==> verifying the stages zip carries rule-module SOURCES, not just bytecode"
unzip -l "${BUILD}/stages.zip" | grep -E 'inventory/(versions|identity|confidence|classify|resolve)\.py' \
  || { echo "FAIL: rule module sources are missing from the zip"; exit 1; }
echo "    ok - all five Stage 2 rule modules present as .py"

echo
echo "==> assembling the upgrade lane's source zip (CodeBuild, not Lambda)"
#
# NOT A LAMBDA PACKAGE, which is why it is assembled here rather than by `assemble` above. It has
# no wheels and its entry point is a buildspec at the archive root, so its layout is CodeBuild's
# (`buildspec.yml` at the top) rather than Lambda's.
#
# THE FILE LIST IS ENUMERATED AND THAT IS THE REVIEW SURFACE. This is the artifact that performs
# every mutation this system makes, so what is in it is what can run against a cluster. A
# `cp -R` of the package would sweep in every stage and the harness proxies --
# code with no business holding a cluster-write identity, shipped into the one artifact that does.
#
# DETERMINISTIC, like every other zip here: sorted names, `zip -X` to drop extra attributes, so
# identical sources produce identical bytes and Terraform does not redeploy on noise.
UPGRADE_DIR="${BUILD}/upgrade-lane"
rm -rf "${UPGRADE_DIR}"
mkdir -p "${UPGRADE_DIR}/eks_addon_readiness/upgrade_lane"

# THE BUILDSPEC IS DERIVED, NOT COPIED, and the reviewed file is untouched. `upgrade_buildspec.yml`
# is what the tests parse and a person reviews; `scripts/lane_script.py` writes its four phase
# blocks verbatim into `lane.sh` as functions, and a `buildspec.yml` whose every phase is one
# `bash ./lane.sh <phase>`. CodeBuild echoes each command before running it, so with the blocks
# inline a lane's log opened with 700 lines of its own source and the ten lines saying what it
# did were buried. Now the log is the lane's output. See the module's header for why this changes
# no execution semantics.
( cd "${ROOT}" && "${BRIEF_PY[@]}" scripts/lane_script.py "${UPGRADE_DIR}" ) \
  || { echo "FAIL: could not derive lane.sh from infra/terraform/modules/platform/upgrade_buildspec.yml"; exit 1; }
bash -n "${UPGRADE_DIR}/lane.sh" \
  || { echo "FAIL: the derived lane.sh does not parse; the buildspec's blocks changed shape"; exit 1; }
cp "${ROOT}/infra/terraform/modules/platform/upgrade_capture.sh"    "${UPGRADE_DIR}/capture.sh"
chmod +x "${UPGRADE_DIR}/capture.sh" "${UPGRADE_DIR}/lane.sh"

# The four root modules `import eks_addon_readiness.upgrade_lane` actually pulls in:
# `__init__.py` imports `knowledge`, `models` and `snapshot`, and all four need pydantic alone.
for f in __init__.py models.py knowledge.py snapshot.py; do
  cp "${ROOT}/eks_addon_readiness/${f}" "${UPGRADE_DIR}/eks_addon_readiness/${f}"
done
# A MODULE ADDED TO THIS LIST MUST BRING NO NEW PACKAGE WITH IT, and that is now CHECKED rather than
# reviewed. `tests/test_the_upgrade_lane_bundle_carries_what_it_imports.py` walks the import closure
# of the lane's entry point and fails when this list does not cover it.
#
# THAT TEST EXISTS BECAUSE THIS LIST DRIFTED AND THE DRIFT REACHED THE ACCOUNT. `addon_version.py`
# was added to the package and not to this loop, while the shipped `__main__.py` and `remediate.py`
# imported it -- so the deployed lane died on ImportError at the first
# `python3 -m eks_addon_readiness.upgrade_lane`, which is EARLIER than the bug that change fixed. A
# file list described as the review surface is precisely the thing a test should hold, because a
# review surface only works when somebody looks at it.
#
# `naming.py` IS DELIBERATELY ABSENT. Nothing the lane runs imports it: it carries the add-on-name to
# workload-identity reconciliation used by `stages/upgrade_triage.py`, which is a LAMBDA, and Lambda
# packages are assembled by `cp -R` of the whole package. Adding it here would ship a module into the
# cluster-writing artifact that nothing in that artifact calls.
for f in __init__.py __main__.py fence.py models.py diagnose.py remediate.py validate.py \
         keys.py report_page.py report_view.py addon_version.py chart_version.py; do
  cp "${ROOT}/eks_addon_readiness/upgrade_lane/${f}" \
     "${UPGRADE_DIR}/eks_addon_readiness/upgrade_lane/${f}"
done

# THERE ARE NO PER-COMPONENT FENCE FILES TO CARRY ANY MORE, and this check used to REQUIRE them.
# The lane's RBAC was generated per component from that release's rendered manifest into
# `infra/terraform/modules/upgrade-lanes/lanes/*.json`, and those files travelled inside this zip so a fence could
# not move independently of the code it bounded. The lane is now ONE type-scoped role serving every
# add-on, so a lane is its release name and namespace -- passed to `fence-check` as flags by the
# buildspec, from values the state machine already holds -- and the kinds it may apply are the
# reviewed generic set in `fence.py`, which is in the zip because the module is.
#
# THE CHECK IS GONE RATHER THAN RELAXED because it would now be asserting the wrong thing. It
# existed to stop a zip deploying a stage that could upgrade nothing; with the generic role, no
# per-component file is needed for any component, so its absence is correct rather than a defect.
# What replaces it as the review surface is the file list above plus
# `tests/test_the_generic_lane_grants_what_the_code_fence_expects.py`, which holds the Terraform
# grants and `FENCED_KINDS` to each other.

rm -f "${BUILD}/upgrade-lane.zip"
# Same fixed timestamp as `assemble`, so this zip is byte-reproducible too.
find "${UPGRADE_DIR}" -exec touch -h -t 198001010000 {} +
( cd "${UPGRADE_DIR}" && find . -type f | sort | zip -q -X -@ "${BUILD}/upgrade-lane.zip" )
echo "    ${BUILD}/upgrade-lane.zip  $(du -h "${BUILD}/upgrade-lane.zip" | cut -f1)"

echo
echo "==> verifying the upgrade lane zip carries a buildspec and the fence code"
# THE LISTING IS TAKEN ONCE AND GREPPED FROM A VARIABLE, NOT PIPED. `unzip -l zip | grep -q x`
# under `set -o pipefail` is a flake: `grep -q` exits at its first match, `unzip` is still writing
# and dies of SIGPIPE (141), and pipefail reports the pipeline failed -- so a member that IS in the
# zip was reported missing whenever the timing landed that way (twice on 2026-10-13, on
# `buildspec.yml` and on `remediate.py`, both passing on an immediate retry).
LANE_LISTING="$(unzip -l "${BUILD}/upgrade-lane.zip")"
for member in buildspec.yml lane.sh capture.sh \
    eks_addon_readiness/upgrade_lane/fence.py eks_addon_readiness/upgrade_lane/remediate.py; do
  grep -q " ${member}$" <<<"${LANE_LISTING}" \
    || { echo "FAIL: ${member} is missing from upgrade-lane.zip"; exit 1; }
done

# THE OPPOSITE ASSERTION TO THE ONE THAT USED TO BE HERE. This step required `lanes/*.json` to be in
# the zip, because the lane's RBAC was generated per component and the fence had to travel with the
# code it bounded. The lane is now one type-scoped role for every add-on, so those files do not exist
# and their absence is correct -- but a stale zip built before that change WOULD still carry them,
# and it would also carry the old `fence-check --fence <file>` CLI while the buildspec passes
# `--component/--release/--namespace`. That mismatch fails at argument parsing in the first lane,
# after a person has already approved the upgrade.
#
# So the check now refuses the STALE shape rather than requiring it, which is the direction that
# catches a zip nobody rebuilt.
if grep -q "lanes/.*\.json$" <<<"${LANE_LISTING}"; then
  echo "FAIL: upgrade-lane.zip carries per-component lane fences under lanes/." >&2
  echo "      Those were retired when the lane became one generic type-scoped role, so a zip" >&2
  echo "      containing them was built from pre-change sources and also carries the old" >&2
  echo "      'fence-check --fence <file>' CLI. The buildspec now passes --component/--release/" >&2
  echo "      --namespace, so that zip fails at argument parsing in the first lane." >&2
  exit 1
fi
if ! grep -q "fence-check" "${UPGRADE_DIR}/lane.sh" \
  || ! grep -q -- "--component" "${UPGRADE_DIR}/lane.sh"; then
  echo "FAIL: the buildspec's fence-check call does not pass --component, so it is either" >&2
  echo "      the pre-change spelling or the check was dropped. The fence is the only thing" >&2
  echo "      keeping one component's upgrade off its neighbours." >&2
  exit 1
fi
echo "    ok - buildspec stub, lane script, capture script and fence code present; no retired fence files"

# ---------------------------------------------------------------------------
# IS WHAT IS DEPLOYED WHAT WAS JUST BUILT?
#
# THIS EXISTS BECAUSE A FORGOTTEN RUN OF THIS SCRIPT COST TWO PIPELINE RUNS AND TWO HUMAN APPROVALS.
# The lane's buildspec lives INSIDE upgrade-lane.zip, and `aws_s3_object.upgrade_source` only
# re-uploads when that zip's hash changes. So editing the buildspec, committing it and running
# `terraform apply` produces a clean plan, a successful apply, and a cluster still running the old
# buildspec. Both times the symptom was a refusal that made no sense against the source:
#
#   run 1  `fence-check --fence lanes/<component>.json` -- a CLI flag the source had already dropped
#   run 2  `can-i: a wildcard write (expect no)` -- a preflight assertion the source had deleted,
#          which cluster-admin makes false, so it refused every lane
#
# NO TEST CAN CATCH THIS. The suite reads the working tree, so it asserted correctly that those
# checks were gone from the source while the deployed artifact still carried them. Only something
# that runs after a build and looks at S3 can compare the two, which is why it lives here.
#
# IT COMPARES MEMBERS, NOT THE ZIP'S BYTES. The archive was once NOT byte-reproducible: copied
# sources carried fresh mtimes on every run, and `zip -X` strips extra attributes without
# normalising those. Two builds from identical sources gave
#
#     build A  J+6Bi7IWd9xY6CZIwzi7aZEXshJMb/keRe11KP3uNGo=
#     build B  zzHgHvISWrEro5ULH2stneLOXYkPasmr1UNszW6rYps=
#
# Every file is now stamped with a fixed time before zipping, so the bytes are reproducible, but a
# member comparison still says WHICH file drifted, which a byte comparison cannot. A byte check that
# warned after every rebuild would also be a warning people learn to skip, the failure this check
# exists to prevent. So it hashes the files whose staleness actually caused the two incidents.
#
# IT WARNS RATHER THAN FAILS, deliberately. This script also runs with no AWS credentials and in CI
# before anything is deployed, where "deployed does not match" is the normal and correct state.
echo "==> comparing the built lane zip against what is deployed"
DRIFT_MEMBERS="buildspec.yml lane.sh eks_addon_readiness/upgrade_lane/fence.py eks_addon_readiness/upgrade_lane/__main__.py"
if ! command -v aws >/dev/null 2>&1; then
  echo "    skipped - no aws CLI on PATH"
elif ! ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
     || [ -z "${ACCOUNT}" ] || [ "${ACCOUNT}" = "None" ]; then
  echo "    skipped - no usable AWS credentials"
else
  DEPLOYED_ZIP="$(mktemp)"
  # Same name the platform module derives: <name_prefix>-<account>-<region>-an.
  DRIFT_PREFIX="${NAME_PREFIX:-eks-addon-upgrades}"
  DRIFT_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
  if aws s3 cp "s3://${DRIFT_PREFIX}-${ACCOUNT}-${DRIFT_REGION}-an/upgrade-lane/source.zip" \
       "${DEPLOYED_ZIP}" --region "${DRIFT_REGION}" --only-show-errors 2>/dev/null; then
    DEPLOYED_DIR="$(mktemp -d)"
    ( cd "${DEPLOYED_DIR}" && unzip -qo "${DEPLOYED_ZIP}" )
    DRIFTED=""
    for member in ${DRIFT_MEMBERS}; do
      # A member the deployed zip does not carry at all IS drift, not a crash: under `set -eo
      # pipefail` an `openssl` on a missing file would otherwise abort this whole script.
      if [ ! -f "${DEPLOYED_DIR}/${member}" ]; then DRIFTED="${DRIFTED} ${member}"; continue; fi
      LOCAL_SUM="$(openssl dgst -sha256 -binary "${UPGRADE_DIR}/${member}" 2>/dev/null | base64)"
      REMOTE_SUM="$(openssl dgst -sha256 -binary "${DEPLOYED_DIR}/${member}" 2>/dev/null | base64)"
      [ "${LOCAL_SUM}" = "${REMOTE_SUM}" ] || DRIFTED="${DRIFTED} ${member}"
    done
    if [ -z "${DRIFTED}" ]; then
      echo "    ok - the deployed lane runs the buildspec and CLI just built"
    else
      echo "    WARNING: the DEPLOYED lane differs from this build in:${DRIFTED}" >&2
      echo "             The next lane will run the deployed copy, not the one just built, so a" >&2
      echo "             refusal you get may not match the source you are reading. Apply" >&2
      echo "             infra/terraform/modules/platform to upload this zip before starting a run." >&2
    fi
    rm -rf "${DEPLOYED_DIR}"
  else
    echo "    skipped - no deployed source.zip to compare against"
  fi
  rm -f "${DEPLOYED_ZIP}"
fi
