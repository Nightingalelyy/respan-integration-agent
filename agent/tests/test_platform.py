from __future__ import annotations

import json
import os
import ssl
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from respan_integration_agent.platform import (
    OFFICIAL_RESPAN_API_ORIGIN,
    BackendAuthenticationError,
    BackendNotFoundError,
    BackendRateLimitError,
    BackendRedirectError,
    BackendRequestError,
    BackendSchemaError,
    BackendTransportError,
    RespanPlatformClient,
    TraceBackend,
    _HttpsTransport,
    _TransportRequest,
    _TransportResponse,
)


TRACE_A = "a" * 32
TRACE_B = "b" * 32
SPAN_A = "span-a"
SPAN_B = "span-b"
START = datetime(2026, 8, 21, 9, 37, tzinfo=timezone.utc)
END = "2026-08-21T09:39:00+00:00"
FAKE_KEY = "rk-test-not-a-secret"


class FakeTransport:
    def __init__(self, responses: Iterable[_TransportResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.requests: list[_TransportRequest] = []

    def request(self, request: _TransportRequest) -> _TransportResponse:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def response(
    payload: Any = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> _TransportResponse:
    default_headers = {"Content-Type": "application/json; charset=utf-8"}
    default_headers.update(headers or {})
    body = raw if raw is not None else json.dumps(payload).encode()
    return _TransportResponse(status, default_headers, body)


def trace_row(trace_id: str = TRACE_A, **extra: Any) -> dict[str, Any]:
    return {"trace_unique_id": trace_id, "new_server_field": {"kept": True}, **extra}


def span_row(
    unique_id: str = SPAN_A, trace_id: str = TRACE_A, **extra: Any
) -> dict[str, Any]:
    return {
        "id": unique_id,
        "unique_id": unique_id,
        "trace_unique_id": trace_id,
        **extra,
    }


def trace_detail(trace_id: str = TRACE_A) -> dict[str, Any]:
    return {
        "id": trace_id,
        "trace_unique_id": trace_id,
        "span_count": 2,
        "error_count": 0,
        "total_tokens": 41,
        "metadata": {"run_id": "run-1"},
        "new_server_field": [1, 2, 3],
        "span_tree": [
            {
                "id": SPAN_A,
                "unique_id": SPAN_A,
                "trace_unique_id": trace_id,
                "span_parent_id": None,
                "children": [
                    {
                        "id": SPAN_B,
                        "unique_id": SPAN_B,
                        "trace_unique_id": trace_id,
                        "span_parent_id": SPAN_A,
                        "children": [],
                    }
                ],
            }
        ],
    }


def make_client(*items: _TransportResponse | BaseException, **kwargs: Any):
    transport = FakeTransport(items)
    return RespanPlatformClient(FAKE_KEY, transport=transport, **kwargs), transport


def test_protocol_and_trace_list_request_pagination_preserve_additive_fields():
    next_url = f"{OFFICIAL_RESPAN_API_ORIGIN}/api/traces/list/?page=2"
    client, transport = make_client(
        response({"count": 2, "next": next_url, "results": [trace_row()]}),
        response({"count": 1, "next": None, "results": [trace_row(TRACE_B)]}),
    )

    assert isinstance(client, TraceBackend)
    assert client.list_traces_by_run_id("run-1", START, END) == [
        trace_row(),
        trace_row(TRACE_B),
    ]
    assert len(transport.requests) == 2
    first, second = transport.requests
    assert first.method == second.method == "POST"
    assert first.url.startswith(f"{OFFICIAL_RESPAN_API_ORIGIN}/api/traces/list/?")
    assert parse_qs(urlsplit(first.url).query) == {
        "page": ["1"],
        "page_size": ["25"],
        "sort_by": ["-timestamp"],
        "start_time": ["2026-08-21T09:37:00.000000Z"],
        "end_time": ["2026-08-21T09:39:00.000000Z"],
    }
    assert parse_qs(urlsplit(second.url).query)["page"] == ["2"]
    assert first.body == second.body
    assert json.loads(first.body or b"") == {
        "filters": {"metadata__run_id": {"operator": "", "value": ["run-1"]}}
    }
    assert first.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert first.headers["Content-Type"] == "application/json"
    assert first.connect_timeout == 10.0
    assert first.read_timeout == 20.0


def test_span_list_uses_exact_trace_filter_and_locks_returned_trace_id():
    client, transport = make_client(
        response({"count": 1, "next": None, "results": [span_row()]})
    )

    assert client.list_spans_by_trace_id(TRACE_A, START, END) == [span_row()]
    assert urlsplit(transport.requests[0].url).path == "/api/request-logs/list/"
    assert json.loads(transport.requests[0].body or b"") == {
        "filters": {"trace_unique_id": {"operator": "", "value": [TRACE_A]}}
    }

    wrong_client, _ = make_client(
        response({"count": 1, "next": None, "results": [span_row(trace_id=TRACE_B)]})
    )
    with pytest.raises(BackendSchemaError):
        wrong_client.list_spans_by_trace_id(TRACE_A, START, END)


def test_detail_requests_validate_paths_identity_and_nested_tree():
    client, transport = make_client(
        response(trace_detail()),
        response(span_row(metadata={"extra": "kept"})),
    )

    assert client.retrieve_trace(TRACE_A)["new_server_field"] == [1, 2, 3]
    assert client.retrieve_span(SPAN_A)["metadata"] == {"extra": "kept"}
    assert [urlsplit(item.url).path for item in transport.requests] == [
        f"/api/traces/{TRACE_A}/",
        f"/api/request-logs/{SPAN_A}/",
    ]
    assert all(
        item.method == "GET" and item.body is None for item in transport.requests
    )

    wrong_trace, _ = make_client(response(trace_detail(TRACE_B)))
    with pytest.raises(BackendSchemaError):
        wrong_trace.retrieve_trace(TRACE_A)

    wrong_span, _ = make_client(response(span_row(SPAN_B)))
    with pytest.raises(BackendSchemaError):
        wrong_span.retrieve_span(SPAN_A)


def test_detail_rejects_malformed_aliases_and_content_core_types():
    bad_trace = trace_detail()
    bad_trace["id"] = TRACE_B
    client, _ = make_client(response(bad_trace))
    with pytest.raises(BackendSchemaError):
        client.retrieve_trace(TRACE_A)

    client, _ = make_client(
        response({**span_row(), "prompt_messages": "wrong-core-type"})
    )
    with pytest.raises(BackendSchemaError):
        client.retrieve_span(SPAN_A)


@pytest.mark.parametrize(
    ("api_key", "kwargs"),
    [
        ("", {}),
        (" key", {}),
        ("key\nheader", {}),
        ("key\theader", {}),
        ("key-unicode-\N{SNOWMAN}", {}),
        (FAKE_KEY, {"connect_timeout": 0}),
        (FAKE_KEY, {"read_timeout": float("inf")}),
        (FAKE_KEY, {"max_response_bytes": 0}),
    ],
)
def test_constructor_rejects_unsafe_credentials_and_limits(api_key, kwargs):
    with pytest.raises(ValueError):
        RespanPlatformClient(api_key, transport=FakeTransport([]), **kwargs)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("retrieve_trace", ("A" * 32,)),
        ("retrieve_trace", ("../trace",)),
        ("retrieve_span", ("../span",)),
        ("retrieve_span", ("span/child",)),
        ("list_traces_by_run_id", ("run id", START, END)),
        ("list_traces_by_run_id", ("run-1", END, START)),
        (
            "list_traces_by_run_id",
            ("run-1", "2026-08-21T09:37:00", END),
        ),
    ],
)
def test_request_identifiers_and_time_ranges_are_validated_before_transport(
    method, args
):
    client, transport = make_client()
    with pytest.raises(ValueError):
        getattr(client, method)(*args)
    assert transport.requests == []


@pytest.mark.parametrize(
    "next_url",
    [
        "https://attacker.invalid/api/traces/list/?page=2",
        "http://api.respan.ai/api/traces/list/?page=2",
        "https://api.respan.ai:443/api/traces/list/?page=2",
        "https://api.respan.ai:bad/api/traces/list/?page=2",
        "https://api.respan.ai/api/request-logs/list/?page=2",
        "https://api.respan.ai/api/traces/list/?page=1",
        "https://api.respan.ai/api/traces/list/?page=2&unknown=value",
        "https://api.respan.ai/api/traces/list/?page=2&page_size=999",
    ],
)
def test_pagination_rejects_untrusted_or_inconsistent_next_links(next_url):
    client, _ = make_client(
        response({"count": 1, "next": next_url, "results": [trace_row()]})
    )
    with pytest.raises((BackendRedirectError, BackendSchemaError)):
        client.list_traces_by_run_id("run-1", START, END)


def test_malformed_pagination_url_has_no_retained_parse_exception():
    client, _ = make_client(
        response(
            {
                "count": 1,
                "next": "https://api.respan.ai:SECRET_PORT/api/traces/list/?page=2",
                "results": [trace_row()],
            }
        )
    )
    with pytest.raises(BackendRedirectError) as caught:
        client.list_traces_by_run_id("run-1", START, END)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_pagination_rejects_duplicate_ids_and_count_smaller_than_results():
    next_url = f"{OFFICIAL_RESPAN_API_ORIGIN}/api/traces/list/?page=2"
    duplicate_client, _ = make_client(
        response({"count": 1, "next": next_url, "results": [trace_row()]}),
        response({"count": 1, "next": None, "results": [trace_row()]}),
    )
    with pytest.raises(BackendSchemaError):
        duplicate_client.list_traces_by_run_id("run-1", START, END)

    bad_count_client, _ = make_client(
        response({"count": 0, "next": None, "results": [trace_row()]})
    )
    with pytest.raises(BackendSchemaError):
        bad_count_client.list_traces_by_run_id("run-1", START, END)


def test_pagination_rejects_positive_empty_and_over_page_size_results():
    positive_empty, _ = make_client(response({"count": 1, "next": None, "results": []}))
    with pytest.raises(BackendSchemaError):
        positive_empty.list_traces_by_run_id("run-1", START, END)

    empty_intermediate, _ = make_client(
        response(
            {
                "count": 0,
                "next": f"{OFFICIAL_RESPAN_API_ORIGIN}/api/traces/list/?page=2",
                "results": [],
            }
        )
    )
    with pytest.raises(BackendSchemaError):
        empty_intermediate.list_traces_by_run_id("run-1", START, END)

    rows = [trace_row(f"{index:032x}") for index in range(26)]
    oversized_page, _ = make_client(
        response({"count": len(rows), "next": None, "results": rows})
    )
    with pytest.raises(BackendSchemaError):
        oversized_page.list_traces_by_run_id("run-1", START, END)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"count": True, "results": []},
        {"count": 1, "results": ["not-an-object"]},
        {"count": 1, "results": [{}]},
        {"count": 1, "results": [trace_row(span_count=True)]},
        {"count": 0, "results": [], "next": 2},
    ],
)
def test_trace_list_rejects_malformed_core_schema(payload):
    client, _ = make_client(response(payload))
    with pytest.raises(BackendSchemaError):
        client.list_traces_by_run_id("run-1", START, END)


@pytest.mark.parametrize(
    "payload",
    [
        {"trace_unique_id": TRACE_A, "span_count": 1, "error_count": 0},
        {
            "trace_unique_id": TRACE_A,
            "span_count": True,
            "error_count": 0,
            "span_tree": [],
        },
        {
            "trace_unique_id": TRACE_A,
            "span_count": 1,
            "error_count": 0,
            "span_tree": [{"id": SPAN_A, "trace_unique_id": TRACE_A}],
        },
        {
            "trace_unique_id": TRACE_A,
            "span_count": 1,
            "error_count": 0,
            "span_tree": [
                {
                    "id": SPAN_A,
                    "trace_unique_id": TRACE_B,
                    "children": [],
                }
            ],
        },
    ],
)
def test_trace_detail_rejects_malformed_core_schema(payload):
    client, _ = make_client(response(payload))
    with pytest.raises(BackendSchemaError):
        client.retrieve_trace(TRACE_A)


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json SECRET_RAW_BODY",
        b'{"count":0,"count":1,"results":[]}',
        b'{"count":NaN,"results":[]}',
        b"\xff",
    ],
)
def test_json_decode_errors_are_redacted_and_suppress_raw_cause(raw):
    client, _ = make_client(response(raw=raw))
    with pytest.raises(BackendSchemaError) as caught:
        client.list_traces_by_run_id("run-1", START, END)
    assert "SECRET_RAW_BODY" not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, BackendRequestError),
        (401, BackendAuthenticationError),
        (403, BackendAuthenticationError),
        (404, BackendNotFoundError),
        (429, BackendRateLimitError),
        (500, BackendTransportError),
        (503, BackendTransportError),
    ],
)
def test_http_errors_are_typed_serializable_and_never_include_body_or_key(
    status, error_type
):
    headers = {"Retry-After": "3.5"} if status == 429 else None
    client, _ = make_client(
        response(
            {"error": f"SECRET_RAW_BODY {FAKE_KEY}"},
            status=status,
            headers=headers,
        )
    )
    with pytest.raises(error_type) as caught:
        client.retrieve_span(SPAN_A)
    error = caught.value
    assert "SECRET_RAW_BODY" not in str(error)
    assert FAKE_KEY not in str(error)
    assert error.as_dict()["status_code"] == status
    assert "SECRET_RAW_BODY" not in json.dumps(error.as_dict())
    if status == 429:
        assert error.retry_after == 3.5
        assert error.as_dict()["retry_after"] == 3.5


def test_redirect_status_is_not_followed():
    client, transport = make_client(response({}, status=302, headers={"Location": "x"}))
    with pytest.raises(BackendRedirectError):
        client.retrieve_span(SPAN_A)
    assert len(transport.requests) == 1


def test_injected_transport_exception_and_oversized_body_are_redacted():
    secret = f"transport failed with {FAKE_KEY} SECRET_RAW_BODY"
    failing_client, _ = make_client(OSError(secret))
    with pytest.raises(BackendTransportError) as caught:
        failing_client.retrieve_span(SPAN_A)
    assert secret not in str(caught.value)
    assert caught.value.operation == "retrieve_span"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None

    oversized_client, _ = make_client(response(raw=b"x" * 11), max_response_bytes=10)
    with pytest.raises(BackendTransportError):
        oversized_client.retrieve_span(SPAN_A)


@pytest.mark.parametrize(
    "bad_response",
    [
        object(),
        _TransportResponse(True, {}, b"{}"),
        _TransportResponse(200, {}, "not-bytes"),
    ],
)
def test_injected_transport_response_shape_is_validated(bad_response):
    class MalformedTransport:
        def request(self, request):
            return bad_response

    client = RespanPlatformClient(FAKE_KEY, transport=MalformedTransport())
    with pytest.raises(BackendTransportError):
        client.retrieve_span(SPAN_A)


def test_injected_header_failure_does_not_remain_in_exception_chain():
    class ExplodingHeaders(dict):
        def items(self):
            raise RuntimeError(f"SECRET_RAW_BODY {FAKE_KEY}")

    client, _ = make_client(_TransportResponse(200, ExplodingHeaders(), b"{}"))
    with pytest.raises(BackendTransportError) as caught:
        client.retrieve_span(SPAN_A)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_non_json_content_type_is_rejected():
    client, _ = make_client(response(span_row(), headers={"Content-Type": "text/html"}))
    with pytest.raises(BackendSchemaError):
        client.retrieve_span(SPAN_A)


def test_conflicting_record_id_aliases_are_rejected():
    payload = span_row()
    payload["unique_id"] = SPAN_B
    client, _ = make_client(response(payload))
    with pytest.raises(BackendSchemaError):
        client.retrieve_span(SPAN_A)


class FakeSocket:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def settimeout(self, value: float) -> None:
        self.timeout = value


class FakeHttpResponse:
    status = 200

    def __init__(self, body: bytes, content_length: str | None = None) -> None:
        self.body = body
        self.content_length = content_length
        self.read_amount: int | None = None

    def getheader(self, name: str) -> str | None:
        return self.content_length if name == "Content-Length" else None

    def read(self, amount: int) -> bytes:
        self.read_amount = amount
        return self.body

    def getheaders(self):
        return [("Content-Type", "application/json")]


class FakeHttpsConnection:
    instances: list["FakeHttpsConnection"] = []
    next_response = FakeHttpResponse(b"{}")

    def __init__(self, host, *, port, timeout, context) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.sock: FakeSocket | None = None
        self.sent: tuple[Any, ...] | None = None
        self.closed = False
        self.instances.append(self)

    def connect(self) -> None:
        self.sock = FakeSocket()

    def request(self, method, target, *, body, headers) -> None:
        self.sent = (method, target, body, headers)

    def getresponse(self) -> FakeHttpResponse:
        return self.next_response

    def close(self) -> None:
        self.closed = True


class FailingHttpsConnection(FakeHttpsConnection):
    def getresponse(self):
        raise OSError(f"SECRET_RAW_BODY {FAKE_KEY}")


def test_https_transport_is_direct_verified_bounded_and_uses_split_timeouts(
    monkeypatch,
):
    monkeypatch.setenv("HTTPS_PROXY", "https://attacker.invalid:8443")
    FakeHttpsConnection.instances.clear()
    FakeHttpsConnection.next_response = FakeHttpResponse(b"{}", content_length="2")
    monkeypatch.setattr(
        "respan_integration_agent.platform.http.client.HTTPSConnection",
        FakeHttpsConnection,
    )
    transport = _HttpsTransport()
    request = _TransportRequest(
        method="GET",
        url=f"{OFFICIAL_RESPAN_API_ORIGIN}/api/traces/{TRACE_A}/?page=1",
        headers={"Authorization": f"Bearer {FAKE_KEY}"},
        connect_timeout=3,
        read_timeout=7,
        max_response_bytes=10,
    )

    result = transport.request(request)
    connection = FakeHttpsConnection.instances[0]
    assert os.environ["HTTPS_PROXY"] == "https://attacker.invalid:8443"
    assert connection.host == "api.respan.ai"
    assert connection.port == 443
    assert connection.timeout == 3
    assert connection.sock is not None and connection.sock.timeout == 7
    assert connection.context.verify_mode == ssl.CERT_REQUIRED
    assert connection.context.check_hostname is True
    assert connection.sent == (
        "GET",
        f"/api/traces/{TRACE_A}/?page=1",
        None,
        {"Authorization": f"Bearer {FAKE_KEY}"},
    )
    assert FakeHttpsConnection.next_response.read_amount == 11
    assert connection.closed is True
    assert result.body == b"{}"


def test_https_transport_rejects_an_unverified_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verified TLS"):
        _HttpsTransport(ssl_context=context)


def test_https_transport_failure_does_not_retain_lower_exception(monkeypatch):
    FailingHttpsConnection.instances.clear()
    monkeypatch.setattr(
        "respan_integration_agent.platform.http.client.HTTPSConnection",
        FailingHttpsConnection,
    )
    transport = _HttpsTransport()
    request = _TransportRequest(
        method="GET",
        url=f"{OFFICIAL_RESPAN_API_ORIGIN}/api/traces/{TRACE_A}/",
        headers={"Authorization": f"Bearer {FAKE_KEY}"},
    )
    with pytest.raises(BackendTransportError) as caught:
        transport.request(request)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("declared_size", ["invalid SECRET_RAW_BODY", "11", "-1"])
def test_https_transport_rejects_invalid_or_large_content_length(
    monkeypatch, declared_size
):
    FakeHttpsConnection.instances.clear()
    FakeHttpsConnection.next_response = FakeHttpResponse(
        b"SECRET_RAW_BODY", content_length=declared_size
    )
    monkeypatch.setattr(
        "respan_integration_agent.platform.http.client.HTTPSConnection",
        FakeHttpsConnection,
    )
    transport = _HttpsTransport()
    request = _TransportRequest(
        method="GET",
        url=f"{OFFICIAL_RESPAN_API_ORIGIN}/api/traces/{TRACE_A}/",
        headers={},
        max_response_bytes=10,
    )

    with pytest.raises(BackendTransportError) as caught:
        transport.request(request)
    assert "SECRET_RAW_BODY" not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert FakeHttpsConnection.instances[0].closed is True
