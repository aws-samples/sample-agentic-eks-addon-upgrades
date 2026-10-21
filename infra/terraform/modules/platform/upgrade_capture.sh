#!/usr/bin/env bash
# Capture one phase of the diagnosis bundle: pod status, pod logs, and the namespace's events.
#
# SEPARATE FROM THE BUILDSPEC BECAUSE IT RUNS TWICE. The `before` and `after` bundles must be the
# SAME reads -- a comparison between two differently-captured bundles is not a comparison -- and
# two copies of this in one YAML file would be two chances to let them diverge.
#
# NO `set -e`. Every read here is expected to be able to fail: a component whose pods are gone has
# no logs, a denied read is a finding, and a `--previous` log does not exist until a container has
# restarted. So each read records its own exit status into `commands.json` and the caller decides.
# `bundle-seal` turns those rows into a manifest where a FAILED read is a file with a non-zero
# status rather than an absence -- which is the difference between "the read was denied" and
# "there was nothing there", and only one of those is evidence.
#
# WHAT IS DELIBERATELY NOT CAPTURED: any Secret, by any command. The lane identity can list
# secrets in its namespace because Helm cannot work without it, and this script does not exercise
# that -- a diagnosis bundle that shipped a neighbour's secret to an S3 prefix would be an
# exfiltration path wearing the face of an audit trail. Also no `kubectl exec` and no port-forward:
# a bundle is made of reads.

BUNDLE="${1:?usage: capture.sh <bundle-dir>}"
mkdir -p "${BUNDLE}/logs"
RECEIPTS="${BUNDLE}/commands.json.lines"
: > "${RECEIPTS}"

# The label selector that IS the lane fence, expressed as a read. `app.kubernetes.io/instance` is
# what Helm stamps on every object it renders for a release, so selecting on it is how this script
# stays inside the component while reading a namespace it shares with everything else. A selector
# on the component NAME would pick up a neighbour whose name happens to match.
SELECTOR="app.kubernetes.io/instance=${RELEASE:-$COMPONENT}"

record() {
  # record <file-name> <command...> -- run it, save stdout to the bundle, note the exit status.
  local name="$1"
  shift
  local status
  "$@" > "${BUNDLE}/${name}" 2>&1
  status=$?
  python3 - "${name}" "$*" "${status}" >> "${RECEIPTS}" <<'PY'
import json, sys
print(json.dumps({"name": sys.argv[1], "command": sys.argv[2], "exit_status": int(sys.argv[3])}))
PY
  return 0
}

if [ "${MANAGED_ADDON:-false}" = "true" ]; then
  # A managed add-on has no Helm release, so its pods are selected by the add-on's own labels.
  # EKS labels them `k8s-app=<addon>` for the ones on this cluster; the selector is passed in so
  # a component whose labels differ does not silently capture nothing.
  SELECTOR="${ADDON_POD_SELECTOR:-k8s-app=${COMPONENT}}"
  record describe-addon.json aws eks describe-addon --region "${TARGET_REGION}" \
    --cluster-name "${CLUSTER_NAME}" --addon-name "${COMPONENT}"
fi

record pods.json kubectl get pods -n "${RELEASE_NAMESPACE}" -l "${SELECTOR}" -o json
# THE WORKLOADS THE PODS BELONG TO. A pod list says which containers are running; only the
# Deployment/DaemonSet/StatefulSet says whether the rollout is complete -- `updatedReplicas`,
# `numberReady`, `observedGeneration`. Same selector, same fence, one read.
record workloads.json kubectl get deployments,daemonsets,statefulsets -n "${RELEASE_NAMESPACE}" \
  -l "${SELECTOR}" -o json

# THE NAMESPACE'S RECENT EVENTS, NOT THE COMPONENT'S. An image that cannot be pulled, a pod that
# cannot be scheduled and a quota that refused a create all land in events attached to objects
# whose names this script does not know yet -- so filtering to the selector here would drop the
# event that explains the failure. Sorted by time, which is how somebody reads them.
record events.json kubectl get events -n "${RELEASE_NAMESPACE}" \
  --sort-by=.lastTimestamp -o json

# ONE FILE PER POD PER CONTAINER, AND THE PREVIOUS CONTAINER TOO. `--previous` is the only place a
# crash-looping container's own account of why it died survives: the current container has just
# started and its log is empty, which is exactly the shape that looks like "no errors".
PODS="$(kubectl get pods -n "${RELEASE_NAMESPACE}" -l "${SELECTOR}" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)"
for pod in ${PODS}; do
  CONTAINERS="$(kubectl get pod "${pod}" -n "${RELEASE_NAMESPACE}" \
    -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{end}' 2>/dev/null)"
  for container in ${CONTAINERS}; do
    record "logs/${pod}.${container}.current.log" \
      kubectl logs "${pod}" -n "${RELEASE_NAMESPACE}" -c "${container}" --tail=2000
    record "logs/${pod}.${container}.previous.log" \
      kubectl logs "${pod}" -n "${RELEASE_NAMESPACE}" -c "${container}" --previous --tail=2000
  done
done

# The receipts, as one JSON array. Written last so a killed capture leaves a bundle with no
# receipts rather than a truncated array `bundle-seal` would refuse to parse -- and an absent
# receipts file is handled (every file becomes `command: unrecorded`) while a corrupt one is not.
python3 - "${RECEIPTS}" "${BUNDLE}/commands.json" <<'PY'
import json, sys
rows = []
with open(sys.argv[1], encoding="utf-8") as lines:
    for line in lines:
        line = line.strip()
        if line:
            rows.append(json.loads(line))
with open(sys.argv[2], "w", encoding="utf-8") as out:
    json.dump(rows, out, indent=2)
PY
rm -f "${RECEIPTS}"
exit 0
