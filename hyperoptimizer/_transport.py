"""Synchronous GraphQL boundary used by the single background writer."""

from __future__ import annotations

import json
import math
import os
import platform
import random
import ssl
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import TracebackType
from urllib.parse import urlsplit, urlunsplit

import httpx

from hyperoptimizer._documents import OPERATIONS, TrackingOperation
from hyperoptimizer._version import __version__

CLIENT_NAME = "hyperoptimizer-python"
MAX_REQUEST_BYTES = 1_048_576


def encode_json(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def ingestion_envelope(body: Mapping[str, object]) -> tuple[bytes, bytes]:
    """Exact wire envelope; append already-frozen event bytes between these pieces."""
    _, document, _ = OPERATIONS["SDKIngest"]
    encoded = encode_json(
        {
            "operationName": "SDKIngest",
            "query": document,
            "variables": {
                "input": {
                    **{key: value for key, value in body.items() if key != "events"},
                    "events": [],
                }
            },
        }
    )
    suffix = b"]}}}"
    return encoded[: -len(suffix)], suffix


class TrackingError(RuntimeError):
    """A request was not acknowledged; raw variables and provider bodies are private."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class _RequestFailure(TrackingError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status=status, code=code)
        self.retry_after = retry_after


def _retry_after(value: object) -> float | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (
                parsedate_to_datetime(str(value)) - datetime.now(timezone.utc)
            ).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


class Transport:
    def __init__(
        self, url: str | None = None, token: str | None = None, *, managed: bool = False
    ) -> None:
        endpoint = url or os.environ.get(
            "HYPEROPTIMIZER_API_URL", "https://graphql.hyperoptimizer.com"
        )
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme == "http"
                and not managed
                and parsed.hostname
                not in {"localhost", "127.0.0.1", "::1", "graphql.127.0.0.1.nip.io"}
            )
        ):
            raise ValueError(
                "Use an HTTPS GraphQL URL without credentials or query parameters; loopback HTTP is supported"
            )
        path = parsed.path.rstrip("/") or "/graphql"
        if not path.endswith("/graphql"):
            raise ValueError("Use a GraphQL origin or an endpoint ending in /graphql")
        self.url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        self.token = token if token is not None else os.environ.get("HYPEROPTIMIZER_API_TOKEN", "")
        if not self.token.strip():
            raise ValueError("Set HYPEROPTIMIZER_API_TOKEN or pass api_token")
        self._scheme = "Tracking" if managed else "Bearer"
        self._user_agent = f"{CLIENT_NAME}/{__version__}/{platform.system() or 'unknown'}".lower()
        # Preserve urllib's platform/OpenSSL trust configuration and environment proxies.
        self._client = httpx.Client(
            verify=ssl.create_default_context(),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )  # HTTPX transport retries default to zero; the SDK owns retries.

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Transport:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def request(
        self,
        operation: TrackingOperation,
        body: Mapping[str, object],
        *,
        timeout: float = 30,
        retry: bool = True,
        event_payloads: list[bytes] | None = None,
    ) -> dict[str, object]:
        _, document, has_input = OPERATIONS[operation]
        variables = {"input": body} if has_input else {}
        if event_payloads is None:
            payload = encode_json(
                {
                    "operationName": operation,
                    "query": document,
                    "variables": variables,
                }
            )
        else:
            if operation != "SDKIngest":
                raise ValueError("Encoded event payloads require SDKIngest")
            prefix, suffix = ingestion_envelope(body)
            payload = prefix + b",".join(event_payloads) + suffix
        if operation == "SDKIngest" and len(payload) > MAX_REQUEST_BYTES:
            raise TrackingError("GraphQL ingestion request exceeds 1 MiB")
        deadline = time.monotonic() + timeout
        delay = 0.25
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TrackingError("Timed out waiting for tracking acknowledgment")
            wait = delay * random.uniform(0.8, 1.2)
            try:
                return self._attempt(operation, body, payload, min(10, remaining))
            except _RequestFailure as failure:
                if not retry or failure.status not in {
                    None,
                    408,
                    429,
                    500,
                    502,
                    503,
                    504,
                }:
                    raise TrackingError(
                        str(failure), status=failure.status, code=failure.code
                    ) from None
                if failure.retry_after is not None:
                    wait = failure.retry_after
            if time.monotonic() + wait >= deadline:
                raise TrackingError("Tracking retry deadline exceeded") from None
            time.sleep(wait)
            delay = min(delay * 2, 5)

    def _attempt(
        self,
        operation: TrackingOperation,
        body: Mapping[str, object],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        try:
            response = self._client.post(
                self.url,
                content=payload,
                timeout=httpx.Timeout(timeout),
                headers={
                    "Authorization": self._scheme + " " + self.token,
                    "Content-Type": "application/json",
                    "Accept": "application/graphql-response+json, application/json",
                    "User-Agent": self._user_agent,
                },
            )
        except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError):
            raise _RequestFailure("Tracking connection failed") from None
        except httpx.HTTPError:
            raise TrackingError("Tracking HTTP request failed") from None
        if not 200 <= response.status_code < 300:
            raise _RequestFailure(
                f"Tracking request rejected (HTTP {response.status_code})",
                status=response.status_code,
                retry_after=_retry_after(response.headers.get("Retry-After")),
            )
        try:
            result = response.json()
        except (ValueError, UnicodeError):
            raise TrackingError("Invalid GraphQL tracking response") from None
        if not isinstance(result, dict):
            raise TrackingError("Invalid GraphQL tracking response")
        errors = result.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and isinstance(errors[0], dict) else {}
            extension = first.get("extensions", {})
            raw_code = extension.get("code") if isinstance(extension, dict) else None
            code = raw_code if isinstance(raw_code, str) else None
            status = {
                "UNAUTHENTICATED": 401,
                "FORBIDDEN": 403,
                "NOT_FOUND": 404,
                "CONFLICT": 409,
                "RATE_LIMITED": 429,
                "SERVICE_UNAVAILABLE": 503,
                "INTERNAL_SERVER_ERROR": 500,
            }.get(code, 400)
            message = f"GraphQL tracking request rejected ({code or 'invalid response'})"
            # FORBIDDEN carries the API's domain-specific denial reason.
            api_message = first.get("message")
            if code == "FORBIDDEN" and isinstance(api_message, str) and api_message.strip():
                message = api_message
            raise _RequestFailure(
                message,
                status=status,
                code=code,
                retry_after=_retry_after(extension.get("retryAfter"))
                if isinstance(extension, dict)
                else None,
            )
        field = OPERATIONS[operation][0]
        data = result.get("data")
        value = data.get(field) if isinstance(data, dict) else None
        if not isinstance(value, dict):
            raise TrackingError("Missing GraphQL tracking acknowledgment")
        if operation == "SDKIngest":
            accepted, duplicates = value.get("accepted"), value.get("duplicates")
            events = body.get("events")
            if (
                not isinstance(accepted, int)
                or isinstance(accepted, bool)
                or not isinstance(duplicates, int)
                or isinstance(duplicates, bool)
                or accepted < 0
                or duplicates < 0
                or not isinstance(events, list)
                or accepted + duplicates != len(events)
                or value.get("requestId") != body.get("requestId")
                or not isinstance(value.get("requestId"), str)
            ):
                raise TrackingError("Invalid GraphQL batch acknowledgment")
        return value
