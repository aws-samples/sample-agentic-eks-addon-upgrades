"""S3 keys for one run's lanes: the result objects, the diagnosis bundles, and the report.

KEYED ON THE RUN AND THE COMPONENT, NOT ON A DIGEST, and that is a deliberate departure from
`storage/keys.py`. Every key there carries the digests of the inputs that determined the document,
because those documents are PURE FUNCTIONS of their inputs and re-running a stage must overwrite
the same key. Nothing here is a pure function of anything: a lane reads a live cluster and changes
it, so two lanes over the same inputs are two different events and must not collide.

So the run id is the partition, which also makes the whole of one run's evidence one `aws s3 ls`
away -- which is the thing somebody reading a report at 2am actually needs.

THE DOCTESTS ARE THE CONTRACT, as they are in `storage/keys.py`: the buildspec builds the diagnosis
prefix and the lane result key from environment variables the state machine sets, and the report
stage lists them back. Two spellings of one key shape would be a report that cannot find the
results it is about to summarise, and the failure would look like "the lanes wrote nothing".
"""

from __future__ import annotations

# The prefixes, named so a bucket policy or a lifecycle rule can address them. Separate from the
# stage documents' prefixes because their retention is not the same question: a plan is small and
# forever, and a diagnosis bundle is megabytes of pod logs.
LANE_RESULTS = "upgrade-lanes"
DIAGNOSIS = "upgrade-diagnosis"
REPORTS = "upgrade-reports"


def lane_result_key(cluster: str, run_id: str, component: str) -> str:
    """One lane's own account of what it did. Written by the build, read by the report stage.

    >>> lane_result_key("my-eks-cluster", "toast-191932", "metrics-server")
    'upgrade-lanes/cluster=my-eks-cluster/run=toast-191932/metrics-server.json'
    """
    return f"{LANE_RESULTS}/cluster={cluster}/run={run_id}/{component}.json"


def diagnosis_prefix(cluster: str, run_id: str, component: str) -> str:
    """Where one component's bundles live. `before/` and `after/` sit under it.

    NO PHASE IN THIS FUNCTION, deliberately: the buildspec appends `before/` or `after/` itself,
    and a caller that wanted one of them has to say which. A prefix that defaulted to a phase is
    how an `after` bundle ends up overwriting the `before` one it was supposed to be compared to.

    >>> diagnosis_prefix("my-eks-cluster", "toast-191932", "coredns")
    'upgrade-diagnosis/cluster=my-eks-cluster/run=toast-191932/component=coredns'
    """
    return f"{DIAGNOSIS}/cluster={cluster}/run={run_id}/component={component}"


def upgrade_report_key(cluster: str, run_id: str) -> str:
    """The run's one report document.

    >>> upgrade_report_key("my-eks-cluster", "toast-191932")
    'upgrade-reports/cluster=my-eks-cluster/run=toast-191932/report.json'
    """
    return f"{REPORTS}/cluster={cluster}/run={run_id}/report.json"
