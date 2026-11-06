import json
import unittest
from unittest.mock import MagicMock, patch

import httpx

from hyperoptimizer._documents import OPERATIONS
from hyperoptimizer._transport import (
    MAX_REQUEST_BYTES,
    TrackingError,
    Transport,
    encode_json,
    ingestion_envelope,
)


class TransportTestCase(unittest.TestCase):
    def mock_transport(self, transport):
        transport.close()
        handler = MagicMock()
        transport._client = httpx.Client(transport=httpx.MockTransport(handler))
        transport._handler = handler
        self.addCleanup(transport.close)
        return handler


class TransportTests(TransportTestCase):
    def test_user_agent_includes_sdk_version_and_system(self):
        for system in ("Linux", "Darwin", "Windows", ""):
            for managed in (False, True):
                with (
                    self.subTest(system=system, managed=managed),
                    patch("hyperoptimizer._transport.platform.system", return_value=system),
                    patch("hyperoptimizer._transport.__version__", "1.2.3-RC1"),
                ):
                    transport = Transport("https://graphql.example", "test", managed=managed)
                    handler = self.mock_transport(transport)
                    handler.return_value = httpx.Response(
                        200, json={"data": {"cliIdentity": {"organizationId": "org"}}}
                    )
                    transport.request("SDKIdentity", {})
                    self.assertEqual(
                        handler.call_args.args[0].headers["User-Agent"],
                        f"hyperoptimizer-python/1.2.3-rc1/{system or 'unknown'}".lower(),
                    )

    def test_ambiguous_responses_retry_identical_ids_and_respect_retry_after(self):
        transport = Transport("https://graphql.example/graphql", "write-only")
        opener = self.mock_transport(transport)
        opener.side_effect = [
            httpx.ReadError("lost response"),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(
                200,
                content=b'{"data":{"ingestRunEvents":{"requestId":"stable-request","accepted":1,"duplicates":0}}}',
            ),
        ]
        body = {"requestId": "stable-request", "events": [{"id": "stable-event"}]}
        with patch("hyperoptimizer._transport.time.sleep") as sleep:
            result = transport.request("SDKIngest", body)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(opener.call_count, 3)
        self.assertTrue(
            all(
                json.loads(call.args[0].content)["variables"]["input"] == body
                for call in opener.call_args_list
            )
        )
        self.assertEqual(sleep.call_args.args, (0,))

    def test_validation_and_authorization_failures_are_not_retried(self):
        for status in [400, 403, 409]:
            with self.subTest(status=status):
                transport = Transport("https://graphql.example", "write-only")
                transport._opener = self.mock_transport(transport)
                transport._handler.return_value = httpx.Response(status)
                with self.assertRaises(TrackingError) as raised:
                    transport.request("SDKIngest", {})
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(transport._handler.call_count, 1)


class GraphQLTransportTests(TransportTestCase):
    def test_graphql_errors_retry_the_identical_envelope_and_never_accept_partial_data(
        self,
    ):
        transport = Transport("https://graphql.example", "hyperoptimizer_write_only")
        transport._opener = self.mock_transport(transport)
        transport._handler.side_effect = [
            httpx.Response(
                200,
                content=b'{"data":{"ingestRunEvents":{"accepted":99}},"errors":[{"message":"private-input","extensions":{"code":"RATE_LIMITED","retryAfter":0}}]}',
            ),
            httpx.Response(
                200,
                content=b'{"data":{"ingestRunEvents":{"requestId":"stable-request","accepted":1,"duplicates":0}}}',
            ),
        ]
        with patch("hyperoptimizer._transport.time.sleep") as sleep:
            result = transport.request("SDKIngest", {"requestId": "stable-request", "events": [{}]})
        self.assertEqual(result["accepted"], 1)
        calls = transport._handler.call_args_list
        self.assertEqual(calls[0].args[0].content, calls[1].args[0].content)
        self.assertEqual(calls[0].args[0].url, "https://graphql.example/graphql")
        self.assertEqual(json.loads(calls[0].args[0].content)["operationName"], "SDKIngest")
        self.assertEqual(sleep.call_args.args, (0,))

    def test_forbidden_errors_preserve_the_api_reason_without_retrying(self):
        for message in (
            "An active paid subscription is required for tracking ingestion",
            "Access denied: missing tracking:write scope",
            "Workspace tracking storage exceeds its quota",
            "Tracking credential does not match this run",
        ):
            with self.subTest(message=message):
                transport = Transport("https://graphql.example", "test")
                handler = self.mock_transport(transport)
                handler.return_value = httpx.Response(
                    200,
                    json={
                        "errors": [
                            {
                                "message": message,
                                "extensions": {"code": "FORBIDDEN"},
                            }
                        ]
                    },
                )
                with self.assertRaises(TrackingError) as raised:
                    transport.request("SDKIngest", {})
                self.assertEqual(str(raised.exception), message)
                self.assertEqual(raised.exception.status, 403)
                self.assertEqual(raised.exception.code, "FORBIDDEN")
                self.assertEqual(handler.call_count, 1)

    def test_forbidden_errors_with_missing_or_invalid_messages_use_generic_fallback(self):
        for message in (None, "", "  ", 42, {"unexpected": "object"}):
            with self.subTest(message=message):
                transport = Transport("https://graphql.example", "test")
                handler = self.mock_transport(transport)
                handler.return_value = httpx.Response(
                    200,
                    json={
                        "errors": [
                            {
                                "message": message,
                                "extensions": {"code": "FORBIDDEN"},
                            }
                        ]
                    },
                )
                with self.assertRaises(TrackingError) as raised:
                    transport.request("SDKIngest", {})
                self.assertEqual(
                    str(raised.exception), "GraphQL tracking request rejected (FORBIDDEN)"
                )
                self.assertEqual(handler.call_count, 1)

    def test_authentication_and_internal_error_messages_remain_redacted(self):
        for code in ("UNAUTHENTICATED", "INTERNAL_SERVER_ERROR", "UNKNOWN_ERROR"):
            with self.subTest(code=code):
                transport = Transport("https://graphql.example", "test")
                handler = self.mock_transport(transport)
                handler.return_value = httpx.Response(
                    200,
                    json={
                        "errors": [
                            {
                                "message": "private-provider-details",
                                "extensions": {"code": code},
                            }
                        ]
                    },
                )
                with self.assertRaises(TrackingError) as raised:
                    transport.request("SDKIngest", {}, retry=False)
                self.assertNotIn("private-provider-details", str(raised.exception))
                self.assertEqual(handler.call_count, 1)

    def test_managed_credentials_use_separate_authentication(self):
        transport = Transport("http://graphql-api.namespace.svc:8186", "signed", managed=True)
        transport._opener = self.mock_transport(transport)
        transport._handler.return_value = httpx.Response(
            200, content=b'{"data":{"bindManagedTrackingRun":{"id":"run"}}}'
        )
        transport.request("SDKBind", {})
        request = transport._handler.call_args.args[0]
        self.assertEqual(request.headers["Authorization"], "Tracking signed")
        self.assertEqual(json.loads(request.content)["variables"], {})

    def test_invalid_urls_and_empty_explicit_credentials_do_not_fall_back(self):
        for url in [
            "https://user:secret@graphql.example",
            "https://graphql.example?secret=token",
            "https://graphql.example/api/v1",
            "http://graphql.example",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Transport(url, "hyperoptimizer_test")
        with (
            patch.dict("os.environ", {"HYPEROPTIMIZER_API_TOKEN": "hyperoptimizer_fallback"}),
            self.assertRaises(ValueError),
        ):
            Transport("https://graphql.example", "")


class AcknowledgementTests(TransportTestCase):
    def test_wrong_request_id_and_invalid_counts_never_acknowledge(self):
        for acknowledgement in [
            {"requestId": "another", "accepted": 1, "duplicates": 0},
            {"requestId": "stable", "accepted": True, "duplicates": 0},
            {"requestId": "stable", "accepted": -1, "duplicates": 2},
            {"requestId": "stable", "accepted": 0, "duplicates": 0},
        ]:
            with self.subTest(acknowledgement=acknowledgement):
                transport = Transport("https://graphql.example", "hyperoptimizer_test")
                transport._opener = self.mock_transport(transport)
                transport._handler.return_value = httpx.Response(
                    200,
                    content=encode_json(
                        {
                            "data": {"ingestRunEvents": acknowledgement},
                        }
                    ),
                )
                with self.assertRaisesRegex(TrackingError, "acknowledgment"):
                    transport.request("SDKIngest", {"requestId": "stable", "events": [{}]})
                self.assertEqual(transport._handler.call_count, 1)

    def test_only_selected_document_is_sent_and_exact_wire_byte_limit_is_enforced(self):
        transport = Transport("https://graphql.example", "hyperoptimizer_test")
        transport._opener = self.mock_transport(transport)
        acknowledgement = {"requestId": "stable", "accepted": 1, "duplicates": 0}
        transport._handler.return_value = httpx.Response(
            200, content=encode_json({"data": {"ingestRunEvents": acknowledgement}})
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
        request = transport._handler.call_args.args[0]
        self.assertEqual(len(request.content), MAX_REQUEST_BYTES)
        self.assertEqual(json.loads(request.content)["variables"]["input"], body)
        document = json.loads(request.content)["query"]
        self.assertEqual(document, OPERATIONS["SDKIngest"][1])
        self.assertNotIn("SDKCreateRun", document)
        event["configuration"]["value"] += "x"
        with self.assertRaisesRegex(TrackingError, "1 MiB"):
            transport.request("SDKIngest", body, event_payloads=[encode_json(event)])
        self.assertEqual(transport._handler.call_count, 1)

    def test_incomplete_batch_acknowledgement_cannot_drop_buffered_events(self):
        transport = Transport("https://graphql.example", "hyperoptimizer_test")
        transport._opener = self.mock_transport(transport)
        transport._handler.return_value = httpx.Response(
            200, content=b'{"data":{"ingestRunEvents":{"accepted":0,"duplicates":0}}}'
        )
        with self.assertRaisesRegex(TrackingError, "acknowledgment"):
            transport.request("SDKIngest", {"events": [{"id": "pending"}]})
        self.assertEqual(transport._handler.call_count, 1)


class HTTPXTests(TransportTestCase):
    def test_transient_errors_retry_but_local_protocol_errors_do_not(self):
        for error_type in (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ):
            with self.subTest(error=error_type):
                transport = Transport("https://graphql.example", "secret")
                handler = self.mock_transport(transport)
                handler.side_effect = [
                    error_type("private"),
                    httpx.Response(200, json={"data": {"cliIdentity": {"organizationId": "org"}}}),
                ]
                with patch("hyperoptimizer._transport.time.sleep"):
                    transport.request("SDKIdentity", {})
                self.assertEqual(handler.call_count, 2)
                self.assertEqual(
                    handler.call_args_list[0].args[0].content,
                    handler.call_args_list[1].args[0].content,
                )
        transport = Transport("https://graphql.example", "secret")
        handler = self.mock_transport(transport)
        handler.side_effect = httpx.LocalProtocolError("private")
        with self.assertRaisesRegex(TrackingError, "^Tracking HTTP request failed$"):
            transport.request("SDKIdentity", {})
        self.assertEqual(handler.call_count, 1)

    def test_redirects_and_nonretryable_creation_are_not_replayed(self):
        for status in (302, 307, 308, 503):
            transport = Transport("https://graphql.example", "secret")
            handler = self.mock_transport(transport)
            handler.return_value = httpx.Response(
                status, headers={"Location": "https://other.example"}
            )
            with self.assertRaises(TrackingError):
                transport.request("SDKCreateProject", {}, retry=False)
            self.assertEqual(handler.call_count, 1)

    def test_deadline_prevents_attempts_and_excessive_server_waits(self):
        transport = Transport("https://graphql.example", "secret")
        handler = self.mock_transport(transport)
        with self.assertRaises(TrackingError):
            transport.request("SDKIdentity", {}, timeout=0)
        handler.assert_not_called()
        handler.return_value = httpx.Response(429, headers={"Retry-After": "120"})
        with patch("hyperoptimizer._transport.time.sleep") as sleep:
            with self.assertRaises(TrackingError):
                transport.request("SDKIdentity", {}, timeout=1)
            sleep.assert_not_called()
        self.assertEqual(handler.call_count, 1)
        timeouts = handler.call_args.args[0].extensions["timeout"]
        self.assertTrue(all(0 < value <= 1 for value in timeouts.values()))

    def test_invalid_json_is_not_retried(self):
        transport = Transport("https://graphql.example", "secret")
        handler = self.mock_transport(transport)
        handler.return_value = httpx.Response(200, content=b"not json")
        with self.assertRaisesRegex(TrackingError, "Invalid GraphQL"):
            transport.request("SDKIdentity", {})
        self.assertEqual(handler.call_count, 1)

    def test_transport_context_closes_on_error_and_repeated_close_is_safe(self):
        transport = Transport("https://graphql.example", "secret")
        with self.assertRaises(ValueError), transport:
            raise ValueError("caller failure")
        self.assertTrue(transport._client.is_closed)
        transport.close()
