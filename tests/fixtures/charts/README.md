# Chart render fixtures

Each `*.rendered.json` / `*.rendered.yaml` file here is the output of

    helm template <release> <chart> --version <version> --values <release values>

for the chart and version named in the filename:

| File | Chart | Version | Upstream license |
|------|-------|---------|------------------|
| `cert-manager-v1.13.2.rendered.*` | cert-manager | v1.13.2 | Apache-2.0 |
| `cert-manager-v1.21.2.rendered.*` | cert-manager | v1.21.2 | Apache-2.0 |
| `keda-2.17.2.rendered.*`          | KEDA         | 2.17.2  | Apache-2.0 |
| `keda-2.21.0.rendered.*`          | KEDA         | 2.21.0  | Apache-2.0 |

## The KEDA fixtures are excerpts

The two KEDA renders are **excerpts** of the full `helm template` output. The
one document that dominates the render — the `scaledjobs.keda.sh`
`CustomResourceDefinition`, whose embedded OpenAPI schema is ~0.3–0.7 MB on its
own — has been dropped. Every remaining document is kept **verbatim**, in order.

This is safe for the tests that read these files. The lane fence
(`refuse_a_render_this_code_cannot_read`) inspects only each document's `kind`;
it never reads a CRD's `spec`. The KEDA fixtures are read only by
`tests/test_an_off_lane_object_is_refused_before_any_mutation.py`, which asserts
that (a) the whole render passes the fence and (b) at least one document carries
no `app.kubernetes.io/instance` label — both of which the excerpt still
satisfies (five label-free CRDs remain, and every kept document has a `kind`).
No assertion is weakened by the trim.

The cert-manager renders are kept whole: they are small, and
`test_the_render_must_ship_what_was_approved.py` reads every workload image out
of them.
