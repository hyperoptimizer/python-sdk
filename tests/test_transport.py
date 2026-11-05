import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from hyperoptimizer._documents import OPERATIONS
from hyperoptimizer._transport import (
    MAX_REQUEST_BYTES,
    TrackingError,
    Transport,
    encode_json,
    ingestion_envelope,
)


class TransportTests(unittest.TestCase):
    def test_ambiguous_responses_retry_identical_ids_and_respect_retry_after(self):
        transport = Transport("https://graphql.example/graphql", "write-only")
        opener = MagicMock()
        opener.open.side_effect = [
            URLError("lost response"),
            HTTPError("https://graphql.example", 429, "limited", {"Retry-After": "0"}, None),
            io.BytesIO(
                b'{"data":{"ingestRunEvents":{"requestId":"stable-request","accepted":1,"duplicates":0}}}'
            ),
        ]
        transport._opener = opener
        body = {"requestId": "stable-request", "events": [{"id": "stable-event"}]}
        with patch("hyperoptimizer._transport.time.sleep") as sleep:
            result = transport.request("SDKIngest", body)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(opener.open.call_count, 3)
        self.assertTrue(
            all(
                json.loads(call.args[0].data)["variables"]["input"] == body
                for call in opener.open.call_args_list
            )
        )
        self.assertEqual(sleep.call_args.args, (0,))

    def test_validation_and_authorization_failures_are_not_retried(self):
        for status in [400, 403, 409]:
            with self.subTest(status=status):
                transport = Transport("https://graphql.example", "write-only")
                transport._opener = MagicMock()
                transport._opener.open.side_effect = HTTPError(
                    "https://graphql.example", status, "rejected", {}, None
                )
                with self.assertRaises(TrackingError) as raised:
                    transport.request("SDKIngest", {})
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(transport._opener.open.call_count, 1)


class GraphQLTransportTests(unittest.TestCase):
    def test_graphql_errors_retry_the_identical_envelope_and_never_accept_partial_data(
        self,
    ):
        transport = Transport("https://graphql.example", "ho_write_only")
        transport._opener = MagicMock()
        transport._opener.open.side_effect = [
            io.BytesIO(
                b'{"data":{"ingestRunEvents":{"accepted":99}},"errors":[{"message":"private-input","extensions":{"code":"RATE_LIMITED","retryAfter":0}}]}'
            ),
            io.BytesIO(
                b'{"data":{"ingestRunEvents":{"requestId":"stable-request","accepted":1,"duplicates":0}}}'
            ),
        ]
        with patch("hyperoptimizer._transport.time.sleep") as sleep:
            result = transport.request("SDKIngest", {"requestId": "stable-request", "events": [{}]})
        self.assertEqual(result["accepted"], 1)
        calls = transport._opener.open.call_args_list
        self.assertEqual(calls[0].args[0].data, calls[1].args[0].data)
        self.assertEqual(calls[0].args[0].full_url, "https://graphql.example/graphql")
        self.assertEqual(json.loads(calls[0].args[0].data)["operationName"], "SDKIngest")
        self.assertEqual(sleep.call_args.args, (0,))

    def test_graphql_authorization_errors_are_permanent_and_redacted(self):
        transport = Transport("https://graphql.example", "ho_write_only")
        transport._opener = MagicMock()
        transport._opener.open.return_value = io.BytesIO(
            b'{"errors":[{"message":"private-input","extensions":{"code":"FORBIDDEN"}}]}'
        )
        with self.assertRaises(TrackingError) as raised:
            transport.request("SDKIngest", {})
        self.assertEqual(raised.exception.code, "FORBIDDEN")
        self.assertNotIn("private-input", str(raised.exception))
        self.assertEqual(transport._opener.open.call_count, 1)

    def test_managed_credentials_use_separate_authentication(self):
        transport = Transport("http://graphql-api.namespace.svc:8186", "signed", managed=True)
        transport._opener = MagicMock()
        transport._opener.open.return_value = io.BytesIO(
            b'{"data":{"bindManagedTrackingRun":{"id":"run"}}}'
        )
        transport.request("SDKBind", {})
        request = transport._opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Tracking signed")
        self.assertEqual(json.loads(request.data)["variables"], {})

    def test_invalid_urls_and_empty_explicit_credentials_do_not_fall_back(self):
        for url in [
            "https://user:secret@graphql.example",
            "https://graphql.example?secret=token",
            "https://graphql.example/api/v1",
            "http://graphql.example",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Transport(url, "ho_test")
        with (
            patch.dict("os.environ", {"HYPEROPTIMIZER_API_TOKEN": "ho_fallback"}),
            self.assertRaises(ValueError),
        ):
            Transport("https://graphql.example", "")


class AcknowledgementTests(unittest.TestCase):
    def test_wrong_request_id_and_invalid_counts_never_acknowledge(self):
        for acknowledgement in [
            {"requestId": "another", "accepted": 1, "duplicates": 0},
            {"requestId": "stable", "accepted": True, "duplicates": 0},
            {"requestId": "stable", "accepted": -1, "duplicates": 2},
            {"requestId": "stable", "accepted": 0, "duplicates": 0},
        ]:
            with self.subTest(acknowledgement=acknowledgement):
                transport = Transport("https://graphql.example", "ho_test")
                transport._opener = MagicMock()
                transport._opener.open.return_value = io.BytesIO(
                    encode_json(
                        {
                            "data": {"ingestRunEvents": acknowledgement},
                        }
                    )
                )
                with self.assertRaisesRegex(TrackingError, "acknowledgment"):
                    transport.request("SDKIngest", {"requestId": "stable", "events": [{}]})
                self.assertEqual(transport._opener.open.call_count, 1)

    def test_only_selected_document_is_sent_and_exact_wire_byte_limit_is_enforced(self):
        transport = Transport("https://graphql.example", "ho_test")
        transport._opener = MagicMock()
        acknowledgement = {"requestId": "stable", "accepted": 1, "duplicates": 0}
        transport._opener.open.return_value = io.BytesIO(
            encode_json({"data": {"ingestRunEvents": acknowledgement}})
        )
        body = {
            "events": [],
            "requestId": "stable",
        }  # Envelope does not depend on key order.
        prefix, suffix = ingestion_envelope(body)
        event = {"configuration": {"id": "event", "path": "unicode/😀", "value": ""}}
        base = len(prefix) + len(encode_json(event)) + len(suffix)
        event["configuration"]["value"] = "x" * (MAX_REQUEST_BYTES - base)
        body["events"] = [event]
        transport.request("SDKIngest", body, event_payloads=[encode_json(event)])
        request = transport._opener.open.call_args.args[0]
        self.assertEqual(len(request.data), MAX_REQUEST_BYTES)
        self.assertEqual(json.loads(request.data)["variables"]["input"], body)
        document = json.loads(request.data)["query"]
        self.assertEqual(document, OPERATIONS["SDKIngest"][1])
        self.assertNotIn("SDKCreateRun", document)
        event["configuration"]["value"] += "x"
        with self.assertRaisesRegex(TrackingError, "1 MiB"):
            transport.request("SDKIngest", body, event_payloads=[encode_json(event)])
        self.assertEqual(transport._opener.open.call_count, 1)

    def test_incomplete_batch_acknowledgement_cannot_drop_buffered_events(self):
        transport = Transport("https://graphql.example", "ho_test")
        transport._opener = MagicMock()
        transport._opener.open.return_value = io.BytesIO(
            b'{"data":{"ingestRunEvents":{"accepted":0,"duplicates":0}}}'
        )
        with self.assertRaisesRegex(TrackingError, "acknowledgment"):
            transport.request("SDKIngest", {"events": [{"id": "pending"}]})
        self.assertEqual(transport._opener.open.call_count, 1)
