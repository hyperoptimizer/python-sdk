# HyperOptimizer Python tracking

Python 3.10+ with no runtime dependencies. All SDK calls use the new `/graphql`
endpoint; there is no REST or old-API fallback. Install the published package (after its first release), or use `pip install .` from this repository:

```sh
pip install hyperoptimizer==0.1.0
export HYPEROPTIMIZER_API_URL=http://127.0.0.1:8186
export HYPEROPTIMIZER_APP_URL=http://127.0.0.1:3003
# Supply a key from the new dashboard's API Keys page:
export HYPEROPTIMIZER_API_TOKEN=ho_your_org_token
```

The default deployment target is `https://graphql.hyperoptimizer.com/graphql`.
The first public package release is pending. Both an origin and an explicit
`/graphql` endpoint are accepted. Remote public endpoints require HTTPS; loopback
HTTP is supported. Tokens and caller-provided organization headers do not select
arbitrary organizations: the server derives ownership from the verified credential.

```python
from hyperoptimizer import Run
from hyperoptimizer.projects import create_project
from hyperoptimizer.types import Histogram

project = create_project(name="project-x")
with Run(project=project.id, experiment_name="training") as run:
    run.log_configs({"parameters/learning_rate": 0.001, "dataset": {"version": 2}})
    for step in range(3):
        run.log_metrics({"metrics/accuracy": 0.8 + step * 0.05}, step=step)
        run.log_histograms({"activations": Histogram([0, 1, 2], [4, 6])}, step=step)
    run.add_tags(["baseline"])
    print(run.get_run_url())
```

Use `tracking:write` to create runs and upload, `experiments:write` when resolving
an experiment by name, and `projects:write` for project creation. These do not
require read scopes. With existing project/experiment IDs, `tracking:write` is
sufficient. `create_project(workspace=...)` checks the key's organization before
creating anything. External tracking requires an active paid subscription; it
does not create or bill managed compute.

`HYPEROPTIMIZER_PROJECT_ID` supplies the default project. Select an existing
experiment with `Run(project=..., experiment_id=...)`. Experiment names resolve
idempotently within the project. Set aggregation policies on first creation:

```python
with Run(project=project.id, experiment_name="scoring",
         metric_policies={"loss": "min", "score": "explicit"}) as run:
    run.log_metrics({"loss": 0.25}, step=10)
    run.log_results({"score": 0.9})
```

Policies are immutable per-run snapshots: `last` means highest step; `min`, `max`,
`mean` and write-once `explicit` are supported. Conflicting experiment policies
are rejected. Close freezes every recorded scalar metric in `RunFinalMetric`;
reads never replace a missing frozen value with a live summary.

`log_configs({"parameters/key": value})` uses typed `RunParameter` columns for
string, finite number and boolean values. Other configurations retain structured
JSON, including null. Values are limited to 64 KiB each and 1 MiB total; a run has
at most 1,000 fields. Paths preserve case and slash namespaces, with reserved
`sys`/`system` and unsafe object keys rejected. Managed parameter assignments
cannot be overwritten. Regular/group tags use the unified assignments.

One run has one writer. Resume a lost external run after its five-minute lease
expires using `Run(project=..., experiment_id=..., run_id=..., resume=True)`.
Closed runs cannot reopen. External status comes from the SDK; managed execution
status belongs to the platform.

Uploads occur at least every second or at 500 queued events. The queue holds at
most 10,000 events/16 MiB with bounded backpressure. Request/event identities stay
unchanged during retries. `flush()` and `close()` wait for acknowledgements and
raise `TrackingError` on failure. Repeated close is safe. Context-manager cleanup
preserves the original training exception. Abrupt termination can lose buffered
events; there is no disk queue.

The bulk mutation is `ingestRunEvents`. One ordered batch can mix scalar samples,
histograms, configuration, explicit results and tag changes. Its `@oneOf` input
requires exactly one event variant per entry, with the event UUID inside that
variant. Each batch contains 1–500 entries and fits the 1 MiB HTTP limit, measured
using the actual UTF-8 GraphQL envelope. A final one-entry flush is valid.
Only the selected generated operation is sent. An acknowledgement must match the
request ID and account for every entry before the SDK removes buffered events.
The server commits the entire batch atomically; conflicting replays save nothing.

Steps/counts use lossless GraphQL BigInt strings with the SDK's nonnegative
safe-integer limit. Observation timestamps retain all six microsecond digits.
A path/step sample is immutable: identical replay is acknowledged, conflicting
values roll back the entire batch. Scalars reject booleans, NaN and infinity.
Histograms support 1–256 bins and retain the 100 greatest-step snapshots. Scalar
history stays in TimescaleDB; cumulative summaries survive retention. Retained
payloads/receipts are limited to 1 GiB per organization and accounted on deletion.
Completion and acknowledged replay remain available after subscription loss.

For SDK-mode managed execution, `Run()` consumes injected tracking URL, token and
writer ID. It sends `Authorization: Tracking <JWT>` to the same GraphQL endpoint.
The new API verifies its dedicated issuer/audience/HS256 signature, expiry and
current run/attempt/writer binding. These credentials cannot create projects,
query records or write other runs. Closing telemetry never changes the observed
execution status. The signing key is `GRAPHQL_TRACKING_SIGNING_KEY` and has no
fallback to Clerk, API keys or the old service's secret.

The actual Hatchet/Kubernetes workers have not yet been migrated to issue these
new credentials or report terminal observations. The managed SDK protocol is
tested with explicit fixtures; no actual managed optimization has been run on
this stack. Stdout ingestion, worker finalization/reconciliation and metering
remain part of that separate migration.

Run `pnpm --filter graphql.hyperoptimizer.com test:sdk` for actual Python-client,
HTTP, Prisma and TimescaleDB integration tests. `pnpm check:tracking-sdk` validates
the bundled `operations.graphql` against the exported schema. Unit tests preserve
queue, retry, backpressure and exception behavior. This change publishes no package.
