from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import unicodedata
import uuid
import warnings
from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone
from types import TracebackType
from urllib.parse import urljoin

from hyperoptimizer._transport import (
    MAX_REQUEST_BYTES,
    TrackingError,
    Transport,
    encode_json,
    ingestion_envelope,
)
from hyperoptimizer.types import Aggregation, Histogram, JsonValue

_MAX_QUEUED_EVENTS = 10_000
_MAX_QUEUED_BYTES = 16 * 1024 * 1024
_BACKPRESSURE_TIMEOUT = 30.0


def _path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(unicodedata.category(char) == "Cc" for char in value)
        or any(
            p in {"", ".", "..", "__proto__", "constructor", "prototype"} for p in value.split("/")
        )
        or value.split("/")[0].lower() in {"sys", "system"}
    ):
        raise ValueError("Invalid tracking field path")
    return value


def _number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Metric values must be finite numbers, excluding bool")
    return value


def _step(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53 - 1:
        raise ValueError("Step must be a nonnegative safe integer")
    return value


def _graphql_event(event: dict[str, object]) -> dict[str, object]:
    kind = str(event["type"])
    payload = {key: value for key, value in event.items() if key != "type"}
    if kind in {"scalar", "histogram"}:
        payload["step"] = str(payload["step"])
    if kind == "histogram":
        histogram = payload["value"]
        if not isinstance(histogram, dict):
            raise ValueError("Invalid histogram")
        payload["value"] = {
            "binEdges": histogram["binEdges"],
            "counts": [str(count) for count in histogram["counts"]],
        }
    return {"configuration" if kind == "config" else kind: payload}


class Run:
    """One writer, a bounded in-memory queue, and acknowledged close semantics."""

    def __init__(
        self,
        *,
        project: str | None = None,
        experiment_name: str | None = None,
        experiment_id: str | None = None,
        run_id: str | None = None,
        resume: bool = False,
        name: str | None = None,
        metric_policies: dict[str, Aggregation] | None = None,
        api_url: str | None = None,
        api_token: str | None = None,
    ) -> None:
        self._writer = str(uuid.uuid4())
        with ExitStack() as cleanup:
            managed_url = os.environ.get("HYPEROPTIMIZER_TRACKING_URL")
            self._managed = managed_url is not None
            if self._managed:
                if (
                    api_url is not None
                    or api_token is not None
                    or experiment_name is not None
                    or resume
                    or metric_policies is not None
                ):
                    raise ValueError("Managed runs use the injected destination and credential")
                for provided, env in [
                    (project, "HYPEROPTIMIZER_PROJECT_ID"),
                    (experiment_id, "HYPEROPTIMIZER_EXPERIMENT_ID"),
                    (run_id, "HYPEROPTIMIZER_RUN_ID"),
                ]:
                    if provided is not None and provided != os.environ.get(env):
                        raise ValueError("Destination conflicts with managed execution")
                token = os.environ.get("HYPEROPTIMIZER_TRACKING_TOKEN")
                self._writer = os.environ["HYPEROPTIMIZER_WRITER_ID"]
                if not token:
                    raise ValueError("Managed execution must inject its tracking credential")
                self._transport = Transport(managed_url, token, managed=True)
                cleanup.callback(self._transport.close)
                identity = self._transport.request("SDKBind", {})
            else:
                policies = metric_policies or {}
                for path, policy in policies.items():
                    _path(path)
                    if policy not in {"last", "min", "max", "mean", "explicit"}:
                        raise ValueError("Invalid metric aggregation policy")
                if metric_policies is not None and experiment_id is not None:
                    raise ValueError("Set policies when creating an experiment by name")
                self._transport = Transport(api_url, api_token)
                cleanup.callback(self._transport.close)
                project = project or os.environ.get("HYPEROPTIMIZER_PROJECT_ID")
                if project is None or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", project):
                    raise ValueError("Supply a project ID or set HYPEROPTIMIZER_PROJECT_ID")
                if experiment_id is None:
                    if not experiment_name:
                        raise ValueError("Supply experiment_name or experiment_id")
                    experiment = self._transport.request(
                        "SDKEnsureExperiment",
                        {
                            "projectId": project,
                            "name": experiment_name,
                            "metrics": [
                                {"key": path, "aggregation": policy}
                                for path, policy in policies.items()
                            ],
                        },
                    )
                    experiment_id = str(experiment["id"])
                elif not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", experiment_id):
                    raise ValueError("Invalid experiment ID")
                scope = {"projectId": project, "experimentId": experiment_id}
                if resume:
                    if not run_id or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", run_id):
                        raise ValueError("Resume requires a run ID")
                    identity = self._transport.request(
                        "SDKResume",
                        {**scope, "runId": run_id, "writerId": self._writer},
                    )
                else:
                    if run_id is not None:
                        raise ValueError("Use resume=True to attach to an existing run")
                    body: dict[str, object] = {
                        **scope,
                        "writerId": self._writer,
                        "requestId": str(uuid.uuid4()),
                    }
                    if name is not None:
                        body["name"] = name
                    identity = self._transport.request("SDKCreateRun", body)
            self.id = str(identity["id"])
            self.experiment_id = str(identity["experimentId"])
            self.project_id = str(identity["projectId"])
            self._scope = {
                "projectId": self.project_id,
                "experimentId": self.experiment_id,
                "runId": self.id,
            }
            self._url = urljoin(
                os.environ.get("HYPEROPTIMIZER_APP_URL", "https://app.hyperoptimizer.com"),
                str(identity["url"]),
            )
            self._condition = threading.Condition()
            self._queue: deque[tuple[dict[str, object], bytes]] = deque()
            self._bytes = 0
            self._inflight = False
            self._error: Exception | None = None
            self._closed = False
            self._closing = False
            self._stop = False
            self._flush_requested = False
            self._thread = threading.Thread(
                target=self._upload, name="hyperoptimizer-upload", daemon=True
            )
            self._thread.start()
            cleanup.pop_all()

    def _enqueue(self, events: list[dict[str, object]]) -> None:
        # Encode each wire event once. The parsed copy freezes user-owned nested objects.
        encoded = [encode_json(_graphql_event(event)) for event in events]
        frozen = [(json.loads(payload), payload) for payload in encoded]
        size = sum(len(payload) for payload in encoded)
        if len(frozen) > _MAX_QUEUED_EVENTS or size > _MAX_QUEUED_BYTES:
            raise TrackingError("Logging call exceeds the queue capacity")
        deadline = time.monotonic() + _BACKPRESSURE_TIMEOUT
        with self._condition:
            while True:
                if self._error is not None:
                    raise TrackingError("Background upload failed") from self._error
                if self._closed or self._closing:
                    raise TrackingError("Run is closed or closing")
                if (
                    len(self._queue) + len(frozen) <= _MAX_QUEUED_EVENTS
                    and self._bytes + size <= _MAX_QUEUED_BYTES
                ):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TrackingError("Tracking queue is full")
                self._condition.wait(remaining)
            self._queue.extend(frozen)
            self._bytes += size
            self._condition.notify_all()

    @staticmethod
    def _event(kind: str, **values: object) -> dict[str, object]:
        return {"id": str(uuid.uuid4()), "type": kind, **values}

    def log_configs(self, data: dict[str, JsonValue]) -> None:
        events = []
        for path, value in data.items():
            _path(path)
            if len(encode_json(value)) > 65_536:
                raise ValueError("Configuration value exceeds 64 KiB")
            events.append(self._event("config", path=path, value=value))
        self._enqueue(events)

    def log_metrics(self, data: dict[str, int | float], *, step: int) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        self._enqueue(
            [
                self._event(
                    "scalar",
                    path=_path(path),
                    value=_number(value),
                    step=_step(step),
                    timestamp=timestamp,
                )
                for path, value in data.items()
            ]
        )

    def log_histograms(self, histograms: dict[str, Histogram], *, step: int) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        events = []
        for path, histogram in histograms.items():
            histogram.__post_init__()
            events.append(
                self._event(
                    "histogram",
                    path=_path(path),
                    value={"binEdges": histogram.bin_edges, "counts": histogram.counts},
                    step=_step(step),
                    timestamp=timestamp,
                )
            )
        self._enqueue(events)

    def log_results(self, data: dict[str, int | float]) -> None:
        self._enqueue(
            [
                self._event("result", path=_path(path), value=_number(value))
                for path, value in data.items()
            ]
        )

    def _tags(self, tags: list[str], group_tags: bool, action: str) -> None:
        if not 1 <= len(tags) <= 20 or any(
            not isinstance(t, str) or not t.strip() or len(t) > 48 for t in tags
        ):
            raise ValueError("Supply 1–20 tags of up to 48 characters")
        self._enqueue([self._event("tags", tags=tags, group=group_tags, action=action)])

    def add_tags(self, tags: list[str], *, group_tags: bool = False) -> None:
        self._tags(tags, group_tags, "add")

    def remove_tags(self, tags: list[str], *, group_tags: bool = False) -> None:
        self._tags(tags, group_tags, "remove")

    def _upload(self) -> None:
        try:
            self._upload_events()
        finally:
            with self._condition:
                if self._stop or self._error is not None:
                    self._transport.close()

    def _upload_events(self) -> None:
        last_heartbeat = time.monotonic()
        while True:
            body: dict[str, object] | None
            with self._condition:
                self._condition.wait_for(
                    lambda: self._stop or self._flush_requested or len(self._queue) >= 500,
                    timeout=1,
                )
                if self._stop:
                    return
                if not self._queue:
                    self._flush_requested = False
                    if self._managed or self._closing or time.monotonic() - last_heartbeat < 30:
                        continue
                    body = None
                else:
                    body = {
                        "requestId": str(uuid.uuid4()),
                        "writerId": self._writer,
                        **self._scope,
                    }
                    prefix, suffix = ingestion_envelope(body)
                    chosen: list[dict[str, object]] = []
                    event_payloads: list[bytes] = []
                    size = len(prefix) + len(suffix)
                    for event, payload in self._queue:
                        added = len(payload) + (1 if chosen else 0)
                        if len(chosen) == 500 or size + added > MAX_REQUEST_BYTES:
                            break
                        chosen.append(event)
                        event_payloads.append(payload)
                        size += added
                    if not chosen:
                        self._error = TrackingError("Tracking event exceeds the request capacity")
                        self._condition.notify_all()
                        return
                    body["events"] = chosen
                self._inflight = True
            try:
                if body is None:
                    self._transport.request(
                        "SDKHeartbeat", {**self._scope, "writerId": self._writer}
                    )
                else:
                    # Transport retries this exact body, including all stable identities.
                    self._transport.request("SDKIngest", body, event_payloads=event_payloads)
                with self._condition:
                    if body is not None:
                        for _ in chosen:
                            _, consumed = self._queue.popleft()
                            self._bytes -= len(consumed)
                    self._error = None
                    self._inflight = False
                    last_heartbeat = time.monotonic()
                    self._condition.notify_all()
            except Exception as error:
                with self._condition:
                    self._error = error
                    self._inflight = False
                    self._condition.notify_all()
                return

    def flush(self, *, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            if self._error:
                raise TrackingError("Tracking upload was not acknowledged") from self._error
            self._flush_requested = True
            self._condition.notify_all()
            while self._queue or self._inflight:
                if self._error:
                    raise TrackingError("Tracking upload was not acknowledged") from self._error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TrackingError("Timed out flushing pending tracking events")
                self._condition.wait(remaining)

    def close(self, *, timeout: float = 30, status: str = "succeeded") -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("Status must be succeeded or failed")
        deadline = time.monotonic() + timeout
        with self._condition:
            if self._closed:
                return
            self._closing = True
        self.flush(timeout=timeout)
        self._transport.request(
            "SDKComplete",
            {**self._scope, "writerId": self._writer, "status": status},
            timeout=max(0.001, deadline - time.monotonic()),
        )
        with self._condition:
            self._closed = True
            self._stop = True
            self._condition.notify_all()
        self._thread.join(timeout=max(0, deadline - time.monotonic()))
        if not self._thread.is_alive():
            self._transport.close()

    def get_run_url(self) -> str:
        return self._url

    def __enter__(self) -> Run:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                self.close()
            else:
                try:
                    self.close(status="failed")
                except Exception:
                    warnings.warn(
                        "Tracking close failed while handling the training exception",
                        RuntimeWarning,
                        stacklevel=2,
                    )
        finally:
            with self._condition:
                self._closing = True
                self._stop = True
                self._condition.notify_all()
            # An in-flight worker closes its own client when it exits.
            if not self._thread.is_alive():
                self._transport.close()
