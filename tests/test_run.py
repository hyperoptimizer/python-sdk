import json
import os
import threading
import unittest
from unittest.mock import patch

from hyperoptimizer import Run, TrackingError
from hyperoptimizer._transport import ingestion_envelope
from hyperoptimizer.types import Histogram

PROJECT = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
EXPERIMENT = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
RUN = "01ARZ3NDEKTSV4RRFFQ69G5FAX"


class RunTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.env = patch.dict(os.environ, {"HYPEROPTIMIZER_API_TOKEN": "test"}, clear=True)
        self.env.start()
        self.mock = patch("hyperoptimizer.run.Transport.request", side_effect=self.request)
        self.mock.start()

    def tearDown(self):
        self.mock.stop()
        self.env.stop()

    def request(self, path, body, **kwargs):
        if path == "SDKIngest":
            # Inspect semantic events while the mock observes structured GraphQL inputs.
            events = []
            for event in body["events"]:
                fields = [(kind, value) for kind, value in event.items() if kind != "id"]
                self.assertEqual(len(fields), 1)
                kind, payload = fields[0]
                events.append(
                    {
                        "id": payload["id"],
                        "type": "config" if kind == "configuration" else kind,
                        **payload,
                    }
                )
            body = {**body, "events": events}
        self.calls.append((path, body))
        if path == "SDKEnsureExperiment":
            return {"id": EXPERIMENT}
        return {
            "id": RUN,
            "projectId": PROJECT,
            "experimentId": EXPERIMENT,
            "url": "/run/" + RUN,
        }

    def run_instance(self):
        return Run(project=PROJECT, experiment_name="training")

    def test_full_example_and_idempotent_close(self):
        with self.run_instance() as run:
            run.log_configs({"parameters/learning_rate": 0.001, "dataset": {"version": 2}})
            for step in range(3):
                run.log_metrics({"metrics/loss": 1 / (step + 1)}, step=step)
            run.log_histograms({"layers/1": Histogram([0, 1, 2], [4, 6])}, step=2)
            run.add_tags(["testing"])
            run.add_tags(["group1"], group_tags=True)
            run.remove_tags(["testing"])
            run.log_results({"final": 0.2})
            run.flush()
        run.close()
        events = [e for path, body in self.calls if path == "SDKIngest" for e in body["events"]]
        self.assertEqual(len(events), 10)
        self.assertEqual(len({e["id"] for e in events}), 10)
        self.assertEqual(len([p for p, _ in self.calls if p == "SDKComplete"]), 1)
        self.assertTrue(run.get_run_url().startswith("https://app.hyperoptimizer.com/"))
        with self.assertRaises(TrackingError):
            run.log_metrics({"late": 1}, step=5)

    def test_validates_before_enqueue_and_freezes_input(self):
        with self.run_instance() as run:
            for value in [True, float("nan"), float("inf"), "3"]:
                with self.assertRaises(ValueError):
                    run.log_metrics({"loss": value}, step=0)
            for path in ["/loss", "loss/", "sys/secret", "__proto__"]:
                with self.assertRaises(ValueError):
                    run.log_metrics({path: 1}, step=0)
            config = {"x": [1, 2]}
            run.log_configs(config)
            config["x"].append(3)
        event = next(e for path, body in self.calls if path == "SDKIngest" for e in body["events"])
        self.assertEqual(event["value"], [1, 2])

    def test_upload_failure_is_observable_and_preserves_original_exception(self):
        def fail(path, body, **kwargs):
            if path == "SDKIngest":
                raise TrackingError("failure")
            return self.request(path, body, **kwargs)

        with patch("hyperoptimizer.run.Transport.request", side_effect=fail):
            run = self.run_instance()
            run.log_metrics({"loss": 1}, step=0)
            with self.assertRaises(TrackingError):
                run.close(timeout=2)
            with self.assertWarns(RuntimeWarning):
                with self.assertRaisesRegex(ValueError, "training failed"):
                    with self.run_instance() as run:
                        run.log_metrics({"loss": 1}, step=0)
                        raise ValueError("training failed")

    def test_managed_attachment_uses_injected_endpoint_and_rejects_destination(self):
        with patch.dict(
            os.environ,
            {
                "HYPEROPTIMIZER_TRACKING_URL": "http://tracking:3003",
                "HYPEROPTIMIZER_TRACKING_TOKEN": "signed",
                "HYPEROPTIMIZER_WRITER_ID": "00000000-0000-4000-8000-000000000000",
            },
        ):
            with Run() as run:
                run.log_metrics({"loss": 1}, step=0)
            self.assertEqual(self.calls[0][0], "SDKBind")
            with self.assertRaises(ValueError):
                Run(project=PROJECT)

    def test_exhausted_logging_call_raises_without_discarding_accepted_events(self):
        with self.run_instance() as run:
            run.log_metrics({"first": 1}, step=0)
            with self.assertRaisesRegex(TrackingError, "queue capacity"):
                run.log_metrics({f"field/{i}": 1 for i in range(10_001)}, step=1)
            run.log_metrics({"last": 2}, step=2)
        events = [
            event for path, body in self.calls if path == "SDKIngest" for event in body["events"]
        ]
        self.assertEqual([event["path"] for event in events], ["first", "last"])

    def test_full_queue_times_out_without_dropping_the_inflight_event(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_request(path, body, **kwargs):
            if path == "SDKIngest":
                entered.set()
                if not release.wait(3):
                    raise TrackingError("Test did not release upload")
            return self.request(path, body, **kwargs)

        with (
            patch("hyperoptimizer.run._MAX_QUEUED_EVENTS", 1),
            patch("hyperoptimizer.run._BACKPRESSURE_TIMEOUT", 0.05),
            patch("hyperoptimizer.run.Transport.request", side_effect=slow_request),
        ):
            run = self.run_instance()
            run.log_metrics({"first": 1}, step=0)
            flushing = threading.Thread(target=run.flush)
            flushing.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(TrackingError, "queue is full"):
                    run.log_metrics({"rejected": 2}, step=1)
            finally:
                release.set()
                flushing.join(2)
                run.close(timeout=2)
        events = [
            event for path, body in self.calls if path == "SDKIngest" for event in body["events"]
        ]
        self.assertEqual([event["path"] for event in events], ["first"])

    def test_close_rejects_a_writer_already_waiting_for_queue_space(self):
        entered = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        rejected = threading.Event()
        errors = []

        def slow_request(path, body, **kwargs):
            if path == "SDKIngest":
                entered.set()
                if not release.wait(3):
                    raise TrackingError("Test did not release upload")
            return self.request(path, body, **kwargs)

        with (
            patch("hyperoptimizer.run._MAX_QUEUED_EVENTS", 1),
            patch("hyperoptimizer.run.Transport.request", side_effect=slow_request),
        ):
            run = self.run_instance()
            original_wait = run._condition.wait

            def signal_wait(timeout=None):
                if threading.current_thread().name == "blocked-logger":
                    waiting.set()
                return original_wait(timeout)

            def log_late():
                try:
                    run.log_metrics({"late": 2}, step=1)
                except TrackingError as error:
                    errors.append(str(error))
                    rejected.set()

            run.log_metrics({"first": 1}, step=0)
            flushing = threading.Thread(target=run.flush)
            flushing.start()
            self.assertTrue(entered.wait(2))
            with patch.object(run._condition, "wait", side_effect=signal_wait):
                logger = threading.Thread(target=log_late, name="blocked-logger")
                logger.start()
                self.assertTrue(waiting.wait(2))
                closing = threading.Thread(target=run.close)
                closing.start()
                try:
                    self.assertTrue(rejected.wait(2))
                    self.assertEqual(errors, ["Run is closed or closing"])
                finally:
                    release.set()
                    logger.join(2)
                    flushing.join(2)
                    closing.join(2)
                    run.close(timeout=2)
        events = [
            event for path, body in self.calls if path == "SDKIngest" for event in body["events"]
        ]
        self.assertEqual([event["path"] for event in events], ["first"])

    def test_histogram_constraints(self):
        for edges, counts in [
            ([0, 0], [1]),
            ([0, 1], [-1]),
            ([0, 1], [True]),
            ([0, 1, 2], [1]),
        ]:
            with self.assertRaises(ValueError):
                Histogram(edges, counts)

    def test_batches_respect_500_entries_and_flush_the_final_single_entry(self):
        with self.run_instance() as run:
            run.log_metrics({f"metric/{i}": i for i in range(1001)}, step=1)
        batches = [body["events"] for path, body in self.calls if path == "SDKIngest"]
        self.assertEqual([len(batch) for batch in batches], [500, 500, 1])
        self.assertEqual(
            [event["path"] for batch in batches for event in batch],
            [f"metric/{i}" for i in range(1001)],
        )
        self.assertEqual(self.calls[-1][0], "SDKComplete")

    def test_utf8_envelope_bytes_determine_batching_without_reserializing_events(self):
        wire_requests = []

        def capture(path, body, **kwargs):
            if path == "SDKIngest":
                prefix, suffix = ingestion_envelope(body)
                payloads = kwargs["event_payloads"]
                wire_requests.append(prefix + b",".join(payloads) + suffix)
            return self.request(path, body, **kwargs)

        with (
            patch("hyperoptimizer.run.MAX_REQUEST_BYTES", 950),
            patch("hyperoptimizer.run.Transport.request", side_effect=capture),
        ):
            with self.run_instance() as run:
                run.log_configs({f"unicode/{i}": "😀" * 40 for i in range(6)})
        self.assertGreater(len(wire_requests), 1)
        self.assertTrue(all(len(payload) <= 950 for payload in wire_requests))
        self.assertEqual(
            sum(
                len(json.loads(payload)["variables"]["input"]["events"])
                for payload in wire_requests
            ),
            6,
        )


if __name__ == "__main__":
    unittest.main()
