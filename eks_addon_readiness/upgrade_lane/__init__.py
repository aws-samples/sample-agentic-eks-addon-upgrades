"""The upgrade lane: one approved component, moved by deterministic code, inside a fence.

    in    one approved step, and a just-in-time write identity for one component
    out   one `LaneOutcome`, one diagnosis bundle, and a row in the run's report

**EVERY MUTATION IN THIS PACKAGE IS PERFORMED BY CODE, NEVER BY A MODEL.** `helm upgrade`,
`eks update-addon`, the rollback and the access-entry lifecycle are all deterministic: the
version to move to arrives as data that a person approved and an AWS principal authorised, and
nothing here asks a model what to do at the moment of acting. The upgrade agent's job is to
RESEARCH and TRIAGE -- it reads, it returns a verdict as a validated document, and
`validate.py` and `remediate.py` decide what that verdict permits. A triage that asked for an
action this package cannot name is refused rather than improvised.

**THE LANE IS A FENCE, NOT AN INSTRUCTION.** The hard requirement is that while troubleshooting
one component the run does not touch another, and all four of this cluster's managed add-ons
share `kube-system` with `metrics-server` -- so a namespace is not a boundary here. Two
mechanisms make the fence real, and they cover different verbs:

  RBAC `resourceNames`   get, update, patch, delete, escalate, bind -- restricted to the
                         component's OWN object names and its own Helm release secrets, in
                         pre-created Roles bound to one group per component.
  this package's code    `create`, which RBAC cannot restrict by name at all. `fence.py`
                         refuses to run `helm upgrade` when the manifest it is about to apply
                         carries any object that is not the release's own.

`list` and `watch` are the residual: Kubernetes DOES accept `resourceNames` there, but only
for a client that sends a matching `metadata.name` field selector, and Helm looks its releases
up by LABEL selector. So a name-fenced `list` on secrets would refuse every Helm operation, and
the fence grants an unfenced `list` on secrets in the one namespace instead. See
`fence.py::release_secret_names` for the whole account, and the design document for what
contains it.

**FAIL CLOSED, EVERYWHERE.** An AWS error, a Kubernetes error, an unparseable document and a
missing diagnosis bundle are all REFUSALS. None of them is a skip, and none of them lets the
run proceed as though the component were healthy. The one thing this package will not do is
report a status it did not establish.

**THE DIAGNOSIS IS CAPTURED BEFORE THE ROLLBACK, AND THAT IS ENFORCED RATHER THAN ORDERED.**
`remediate.py::plan_rollback` refuses when the bundle's own manifest is absent, so "a rollback
that leaves nothing to read" cannot happen by forgetting -- it can only happen by deleting a
check.
"""

from __future__ import annotations
