"""Read-only, fail-closed access to the public Respan trace APIs.

The v0 smoke gate needs a deliberately small backend boundary.  This module keeps
authentication, origin validation, pagination, and core response validation out of the
semantic trace policy.  It intentionally uses only the standard library and never puts
response bodies or credentials in exceptions.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit


OFFICIAL_RESPAN_API_ORIGIN = "https://api.respan.ai"

_TRACE_LIST_PATH = "/api/traces/list/"
_TRACE_DETAIL_PATH = "/api/traces/{trace_id}/"
_SPAN_LIST_PATH = "/api/request-logs/list/"
_SPAN_DETAIL_PATH = "/api/request-logs/{unique_id}/"
_PAGE_SIZE = 25
_MAX_PAGES = 100
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
_DEFAULT_READ_TIMEOUT_SECONDS = 20.0
_DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_IDENTIFIER_LENGTH = 255
_MAX_RUN_ID_LENGTH = 128
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

JsonObject = dict[str, Any]
UtcTime = datetime | str


@runtime_checkable
class TraceBackend(Protocol):
    """Backend operations required by the v0 trace gate."""

    def list_traces_by_run_id(
        self, run_id: str, start_time: UtcTime, end_time: UtcTime
    ) -> list[JsonObject]: ...

    def retrieve_trace(self, trace_id: str) -> JsonObject: ...

    def list_spans_by_trace_id(
        self, trace_id: str, start_time: UtcTime, end_time: UtcTime
    ) -> list[JsonObject]: ...

    def retrieve_span(self, unique_id: str) -> JsonObject: ...


class BackendError(RuntimeError):
    """Base class for safe backend failures.

    The message is constructed only from constants and numeric status codes.  In
    particular, URLs, request headers, response bodies, and underlying exception text
    are deliberately excluded.
    """

    code = "backend_error"

    def __init__(self, operation: str, *, status_code: int | None = None) -> None:
        self.operation = operation
        self.status_code = status_code
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"{self.code}: {operation} failed{status}")

    def as_dict(self) -> dict[str, str | int | float]:
        """Return the only fields safe for smoke evidence."""

        result: dict[str, str | int | float] = {
            "code": self.code,
            "operation": self.operation,
        }
        if self.status_code is not None:
            result["status_code"] = self.status_code
        if isinstance(self, BackendRateLimitError) and self.retry_after is not None:
            result["retry_after"] = self.retry_after
        return result


class BackendAuthenticationError(BackendError):
    code = "backend_authentication_error"


class BackendRateLimitError(BackendError):
    code = "backend_rate_limit_error"

    def __init__(
        self,
        operation: str,
        *,
        status_code: int = 429,
        retry_after: float | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(operation, status_code=status_code)


class BackendTransportError(BackendError):
    code = "backend_transport_error"


class BackendRedirectError(BackendTransportError):
    code = "backend_redirect_error"


class BackendRequestError(BackendError):
    code = "backend_request_error"


class BackendNotFoundError(BackendError):
    code = "backend_not_found"


class BackendSchemaError(BackendError):
    code = "backend_schema_error"


@dataclass(frozen=True)
class _TransportRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes | None = field(default=None, repr=False)
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout: float = _DEFAULT_READ_TIMEOUT_SECONDS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES


@dataclass(frozen=True)
class _TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)


class _Transport(Protocol):
    def request(self, request: _TransportRequest) -> _TransportResponse: ...


class _HttpsTransport:
    """Direct HTTPS transport: verified TLS, no proxy lookup, no redirects."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        context = ssl_context or ssl.create_default_context()
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError("the HTTPS transport requires verified TLS")
        self._ssl_context = context

    def request(self, request: _TransportRequest) -> _TransportResponse:
        parsed = _validate_official_url(request.url, expected_path=None)
        target = urlunsplit(("", "", parsed.path, parsed.query, ""))
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            port=443,
            timeout=request.connect_timeout,
            context=self._ssl_context,
        )
        result: _TransportResponse | None = None
        transport_failed = False
        try:
            connection.connect()
            if connection.sock is None:  # pragma: no cover - defensive stdlib invariant
                raise OSError("HTTPS connection has no socket")
            connection.sock.settimeout(request.read_timeout)
            connection.request(
                request.method,
                target,
                body=request.body,
                headers=dict(request.headers),
            )
            response = connection.getresponse()
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                declared_size = _parse_content_length(content_length)
                if declared_size < 0 or declared_size > request.max_response_bytes:
                    raise BackendTransportError("http_response")
            body = response.read(request.max_response_bytes + 1)
            if len(body) > request.max_response_bytes:
                raise BackendTransportError("http_response")
            headers = {key.lower(): value for key, value in response.getheaders()}
            result = _TransportResponse(response.status, headers, body)
        except BackendError:
            raise
        except Exception:
            transport_failed = True
        finally:
            try:
                connection.close()
            except Exception:
                transport_failed = True
        if transport_failed or result is None:
            raise BackendTransportError("http_request")
        return result


class RespanPlatformClient:
    """Authenticated read adapter pinned to the public Respan API origin."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: _Transport | None = None,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = _DEFAULT_READ_TIMEOUT_SECONDS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key
            or api_key != api_key.strip()
            or len(api_key) > 4096
            or any(not 33 <= ord(character) <= 126 for character in api_key)
        ):
            raise ValueError("a valid Respan API key is required")
        self._api_key = api_key
        self._transport = transport or _HttpsTransport()
        self._connect_timeout = _positive_finite_timeout(
            connect_timeout, "connect_timeout"
        )
        self._read_timeout = _positive_finite_timeout(read_timeout, "read_timeout")
        if (
            type(max_response_bytes) is not int
            or max_response_bytes < 1
            or max_response_bytes > 64 * 1024 * 1024
        ):
            raise ValueError("max_response_bytes must be between 1 and 67108864")
        self._max_response_bytes = max_response_bytes

    def list_traces_by_run_id(
        self, run_id: str, start_time: UtcTime, end_time: UtcTime
    ) -> list[JsonObject]:
        marker = _validated_run_id(run_id)
        start, end = _validated_time_range(start_time, end_time)
        body = {
            "filters": {
                "metadata__run_id": {
                    "operator": "",
                    "value": [marker],
                }
            }
        }
        return self._list_all(
            operation="list_traces",
            path=_TRACE_LIST_PATH,
            start_time=start,
            end_time=end,
            body=body,
            item_validator=_validate_trace_list_item,
            identity=_trace_identity,
        )

    def retrieve_trace(self, trace_id: str) -> JsonObject:
        locked_trace_id = _validated_trace_id(trace_id)
        path = _TRACE_DETAIL_PATH.format(trace_id=quote(locked_trace_id, safe=""))
        result = self._request_json("retrieve_trace", "GET", self._url(path), None)
        _validate_trace_detail(result, operation="retrieve_trace")
        if result["trace_unique_id"] != locked_trace_id:
            raise BackendSchemaError("retrieve_trace")
        _validate_tree_trace_ids(result["span_tree"], locked_trace_id)
        return result

    def list_spans_by_trace_id(
        self, trace_id: str, start_time: UtcTime, end_time: UtcTime
    ) -> list[JsonObject]:
        locked_trace_id = _validated_trace_id(trace_id)
        start, end = _validated_time_range(start_time, end_time)
        body = {
            "filters": {
                "trace_unique_id": {
                    "operator": "",
                    "value": [locked_trace_id],
                }
            }
        }
        results = self._list_all(
            operation="list_spans",
            path=_SPAN_LIST_PATH,
            start_time=start,
            end_time=end,
            body=body,
            item_validator=_validate_span_list_item,
            identity=_span_identity,
        )
        if any(item["trace_unique_id"] != locked_trace_id for item in results):
            raise BackendSchemaError("list_spans")
        return results

    def retrieve_span(self, unique_id: str) -> JsonObject:
        locked_unique_id = _validated_span_id(unique_id)
        path = _SPAN_DETAIL_PATH.format(unique_id=quote(locked_unique_id, safe=""))
        result = self._request_json("retrieve_span", "GET", self._url(path), None)
        _validate_span_detail(result, operation="retrieve_span")
        response_ids = {result["id"]}
        if isinstance(result.get("unique_id"), str):
            response_ids.add(result["unique_id"])
        if locked_unique_id not in response_ids:
            raise BackendSchemaError("retrieve_span")
        return result

    def _list_all(
        self,
        *,
        operation: str,
        path: str,
        start_time: str,
        end_time: str,
        body: JsonObject,
        item_validator: Any,
        identity: Any,
    ) -> list[JsonObject]:
        fixed_query = {
            "page_size": str(_PAGE_SIZE),
            "sort_by": "-timestamp",
            "start_time": start_time,
            "end_time": end_time,
        }
        page = 1
        url = self._url(path, {**fixed_query, "page": str(page)})
        encoded_body = _encode_json(body, operation)
        results: list[JsonObject] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        while True:
            if page > _MAX_PAGES or url in seen_urls:
                raise BackendSchemaError(operation)
            seen_urls.add(url)
            envelope = self._request_json(operation, "POST", url, encoded_body)
            page_results, next_url = _validate_page(envelope, operation, item_validator)
            for item in page_results:
                item_id = identity(item, operation)
                if item_id in seen_ids:
                    raise BackendSchemaError(operation)
                seen_ids.add(item_id)
                results.append(item)
            if next_url is None:
                return results
            page += 1
            url = _validated_next_url(
                next_url,
                expected_path=path,
                expected_page=page,
                fixed_query=fixed_query,
                operation=operation,
            )

    def _request_json(
        self, operation: str, method: str, url: str, body: bytes | None
    ) -> JsonObject:
        _validate_official_url(url, expected_path=urlsplit(url).path)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": "respan-integration-agent/0.0.1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = _TransportRequest(
            method=method,
            url=url,
            headers=headers,
            body=body,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
            max_response_bytes=self._max_response_bytes,
        )
        response: _TransportResponse | None = None
        transport_failed = False
        try:
            response = self._transport.request(request)
        except Exception:
            transport_failed = True
        if transport_failed or response is None:
            raise BackendTransportError(operation)
        if (
            not isinstance(response, _TransportResponse)
            or type(response.status) is not int
            or not isinstance(response.headers, Mapping)
            or not isinstance(response.body, bytes)
        ):
            raise BackendTransportError(operation)
        if len(response.body) > self._max_response_bytes:
            raise BackendTransportError(operation)
        normalized_headers: dict[str, str] | None = None
        headers_failed = False
        try:
            normalized_headers = {
                str(key).lower(): str(value) for key, value in response.headers.items()
            }
        except Exception:
            headers_failed = True
        if headers_failed or normalized_headers is None:
            raise BackendTransportError(operation)
        self._raise_for_status(
            operation,
            response.status,
            normalized_headers.get("retry-after"),
        )
        content_type = normalized_headers.get("content-type")
        if content_type is not None and not content_type.lower().startswith(
            "application/json"
        ):
            raise BackendSchemaError(operation)
        return _decode_json_object(response.body, operation)

    @staticmethod
    def _raise_for_status(
        operation: str, status: int, retry_after_header: str | None
    ) -> None:
        if type(status) is not int:
            raise BackendTransportError(operation)
        if status == 200:
            return
        if 300 <= status <= 399:
            raise BackendRedirectError(operation, status_code=status)
        if status in {401, 403}:
            raise BackendAuthenticationError(operation, status_code=status)
        if status == 404:
            raise BackendNotFoundError(operation, status_code=status)
        if status == 429:
            raise BackendRateLimitError(
                operation,
                retry_after=_parse_retry_after(retry_after_header),
            )
        if 500 <= status <= 599:
            raise BackendTransportError(operation, status_code=status)
        raise BackendRequestError(operation, status_code=status)

    @staticmethod
    def _url(path: str, query: Mapping[str, str] | None = None) -> str:
        encoded_query = urlencode(query or {})
        return f"{OFFICIAL_RESPAN_API_ORIGIN}{path}" + (
            f"?{encoded_query}" if encoded_query else ""
        )


def _positive_finite_timeout(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > 120:
        raise ValueError(f"{name} must be between 0 and 120 seconds")
    return result


def _parse_content_length(value: str) -> int:
    parsed = None
    parse_failed = False
    try:
        parsed = int(value)
    except ValueError:
        parse_failed = True
    return -1 if parse_failed or parsed is None else parsed


def _validated_run_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_RUN_ID_LENGTH
        or _RUN_ID_RE.fullmatch(value) is None
    ):
        raise ValueError("run_id has an invalid format")
    return value


def _validated_trace_id(value: str) -> str:
    if not isinstance(value, str) or _TRACE_ID_RE.fullmatch(value) is None:
        raise ValueError("trace_id must be 32 lowercase hexadecimal characters")
    return value


def _validated_span_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_IDENTIFIER_LENGTH
        or _SPAN_ID_RE.fullmatch(value) is None
    ):
        raise ValueError("unique_id has an invalid format")
    return value


def _validated_time_range(start_time: UtcTime, end_time: UtcTime) -> tuple[str, str]:
    start_dt = _as_utc_datetime(start_time, "start_time")
    end_dt = _as_utc_datetime(end_time, "end_time")
    if start_dt >= end_dt:
        raise ValueError("start_time must be earlier than end_time")
    return _format_utc(start_dt), _format_utc(end_dt)


def _as_utc_datetime(value: UtcTime, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = None
        parse_failed = False
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            parse_failed = True
        if parse_failed or parsed is None:
            raise ValueError(f"{name} must be an ISO 8601 timestamp")
    else:
        raise ValueError(f"{name} must be an aware datetime or ISO 8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_official_url(url: str, expected_path: str | None):
    if not isinstance(url, str):
        raise BackendRedirectError("pagination")
    parsed = None
    hostname = None
    port = None
    parse_failed = False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        parse_failed = True
    if parse_failed or parsed is None:
        raise BackendRedirectError("pagination")
    if (
        parsed.scheme != "https"
        or hostname != "api.respan.ai"
        or parsed.netloc != "api.respan.ai"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or (expected_path is not None and parsed.path != expected_path)
    ):
        raise BackendRedirectError("pagination")
    return parsed


def _validated_next_url(
    value: str,
    *,
    expected_path: str,
    expected_page: int,
    fixed_query: Mapping[str, str],
    operation: str,
) -> str:
    parsed = _validate_official_url(value, expected_path=expected_path)
    query = None
    parse_failed = False
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        parse_failed = True
    if parse_failed or query is None:
        raise BackendSchemaError(operation)
    allowed_keys = {*fixed_query, "page"}
    if not query or set(query) - allowed_keys:
        raise BackendSchemaError(operation)
    if query.get("page") != [str(expected_page)]:
        raise BackendSchemaError(operation)
    for key, expected_value in fixed_query.items():
        if key in query and query[key] != [expected_value]:
            raise BackendSchemaError(operation)
    rebuilt_query = {**fixed_query, "page": str(expected_page)}
    return f"{OFFICIAL_RESPAN_API_ORIGIN}{expected_path}?{urlencode(rebuilt_query)}"


def _encode_json(value: JsonObject, operation: str) -> bytes:
    encoded = None
    encode_failed = False
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):  # pragma: no cover - static request bodies
        encode_failed = True
    if encode_failed or encoded is None:  # pragma: no cover - static request bodies
        raise BackendSchemaError(operation)
    return encoded


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise ValueError


def _decode_json_object(body: bytes, operation: str) -> JsonObject:
    value = None
    decode_failed = False
    try:
        decoded = body.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        decode_failed = True
    if decode_failed:
        raise BackendSchemaError(operation)
    if not isinstance(value, dict):
        raise BackendSchemaError(operation)
    return value


def _validate_page(
    value: JsonObject, operation: str, item_validator: Any
) -> tuple[list[JsonObject], str | None]:
    count = value.get("count")
    results = value.get("results")
    next_url = value.get("next")
    if type(count) is not int or count < 0 or not isinstance(results, list):
        raise BackendSchemaError(operation)
    if (
        count < len(results)
        or len(results) > _PAGE_SIZE
        or (count > 0 and not results)
        or (next_url is not None and not results)
        or (next_url is not None and not isinstance(next_url, str))
    ):
        raise BackendSchemaError(operation)
    validated: list[JsonObject] = []
    for item in results:
        if not isinstance(item, dict):
            raise BackendSchemaError(operation)
        item_validator(item, operation)
        validated.append(item)
    return validated, next_url


def _validate_trace_list_item(value: JsonObject, operation: str) -> None:
    trace_id = _required_trace_id(value, "trace_unique_id", operation)
    _validate_trace_alias(value, trace_id, operation)
    _validate_known_fields(value, operation, _TRACE_FIELDS)


def _validate_trace_detail(value: JsonObject, operation: str) -> None:
    trace_id = _required_trace_id(value, "trace_unique_id", operation)
    _validate_trace_alias(value, trace_id, operation)
    _required_nonnegative_int(value, "span_count", operation)
    _required_nonnegative_int(value, "error_count", operation)
    if not isinstance(value.get("span_tree"), list):
        raise BackendSchemaError(operation)
    _validate_known_fields(value, operation, _TRACE_FIELDS)
    _validate_span_tree(value["span_tree"], operation)


def _validate_span_list_item(value: JsonObject, operation: str) -> None:
    _span_identity(value, operation)
    _required_trace_id(value, "trace_unique_id", operation)
    _validate_span_identifiers(value, operation)
    _validate_known_fields(value, operation, _SPAN_FIELDS)
    _validate_json_content_fields(value, operation)


def _validate_span_detail(value: JsonObject, operation: str) -> None:
    if "id" not in value:
        raise BackendSchemaError(operation)
    _validated_response_span_id(value["id"], operation)
    _required_trace_id(value, "trace_unique_id", operation)
    _validate_span_identifiers(value, operation)
    _validate_known_fields(value, operation, _SPAN_FIELDS)
    _validate_json_content_fields(value, operation)


def _trace_identity(value: JsonObject, operation: str) -> str:
    return _required_trace_id(value, "trace_unique_id", operation)


def _span_identity(value: JsonObject, operation: str) -> str:
    for key in ("unique_id", "id"):
        if key in value:
            return _validated_response_span_id(value[key], operation)
    raise BackendSchemaError(operation)


def _required_trace_id(value: JsonObject, key: str, operation: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or _TRACE_ID_RE.fullmatch(candidate) is None:
        raise BackendSchemaError(operation)
    return candidate


def _validated_response_span_id(value: Any, operation: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_IDENTIFIER_LENGTH
        or _SPAN_ID_RE.fullmatch(value) is None
    ):
        raise BackendSchemaError(operation)
    return value


def _required_nonnegative_int(value: JsonObject, key: str, operation: str) -> int:
    candidate = value.get(key)
    if type(candidate) is not int or candidate < 0:
        raise BackendSchemaError(operation)
    return candidate


def _validate_span_tree(tree: list[Any], operation: str) -> None:
    pending = list(tree)
    visited = 0
    while pending:
        node = pending.pop()
        visited += 1
        if visited > 100_000 or not isinstance(node, dict):
            raise BackendSchemaError(operation)
        _span_identity(node, operation)
        _required_trace_id(node, "trace_unique_id", operation)
        _validate_span_identifiers(node, operation)
        children = node.get("children")
        if not isinstance(children, list):
            raise BackendSchemaError(operation)
        pending.extend(children)
        _validate_known_fields(node, operation, _TREE_FIELDS)
        _validate_json_content_fields(node, operation)


def _validate_tree_trace_ids(tree: list[Any], trace_id: str) -> None:
    pending = list(tree)
    while pending:
        node = pending.pop()
        if node["trace_unique_id"] != trace_id:
            raise BackendSchemaError("retrieve_trace")
        pending.extend(node["children"])


def _validate_trace_alias(value: JsonObject, trace_id: str, operation: str) -> None:
    if "id" in value and value["id"] is not None:
        if value["id"] != trace_id:
            raise BackendSchemaError(operation)
    if "root_span_unique_id" in value and value["root_span_unique_id"] is not None:
        _validated_response_span_id(value["root_span_unique_id"], operation)


def _validate_span_identifiers(value: JsonObject, operation: str) -> None:
    for key in ("id", "unique_id", "span_unique_id"):
        if key in value and value[key] is not None:
            _validated_response_span_id(value[key], operation)
    if (
        value.get("id") is not None
        and value.get("unique_id") is not None
        and value["id"] != value["unique_id"]
    ):
        raise BackendSchemaError(operation)


def _validate_json_content_fields(value: JsonObject, operation: str) -> None:
    for key in ("input", "output", "warnings", "properties"):
        if key not in value or value[key] is None:
            continue
        if type(value[key]) not in {str, dict, list, int, float, bool}:
            raise BackendSchemaError(operation)
        if type(value[key]) is float and not math.isfinite(value[key]):
            raise BackendSchemaError(operation)


def _validate_known_fields(
    value: JsonObject,
    operation: str,
    fields: Mapping[str, tuple[type, ...]],
) -> None:
    for key, allowed_types in fields.items():
        if key not in value or value[key] is None:
            continue
        candidate = value[key]
        if bool in allowed_types and isinstance(candidate, bool):
            continue
        if int in allowed_types and type(candidate) is int:
            if candidate < 0:
                raise BackendSchemaError(operation)
            continue
        if float in allowed_types and type(candidate) in {int, float}:
            if not math.isfinite(float(candidate)) or candidate < 0:
                raise BackendSchemaError(operation)
            continue
        if not any(
            allowed is not int
            and allowed is not float
            and allowed is not bool
            and isinstance(candidate, allowed)
            for allowed in allowed_types
        ):
            raise BackendSchemaError(operation)


_TRACE_FIELDS: Mapping[str, tuple[type, ...]] = {
    "id": (str,),
    "trace_unique_id": (str,),
    "root_span_unique_id": (str,),
    "environment": (str,),
    "start_time": (str,),
    "end_time": (str,),
    "duration": (float,),
    "span_count": (int,),
    "llm_call_count": (int,),
    "total_cost": (float,),
    "total_prompt_tokens": (int,),
    "total_completion_tokens": (int,),
    "total_tokens": (int,),
    "error_count": (int,),
    "name": (str,),
    "input": (str,),
    "output": (str,),
    "metadata": (dict,),
    "span_tree": (list,),
}

_SPAN_FIELDS: Mapping[str, tuple[type, ...]] = {
    "id": (str,),
    "unique_id": (str,),
    "span_unique_id": (str,),
    "trace_unique_id": (str,),
    "span_name": (str,),
    "span_parent_id": (str,),
    "timestamp": (str,),
    "start_time": (str,),
    "end_time": (str,),
    "log_type": (str,),
    "status": (str,),
    "status_code": (int,),
    "error_code": (str,),
    "error_message": (str,),
    "prompt_messages": (list,),
    "completion_message": (dict,),
    "completion_messages": (list,),
    "full_request": (dict,),
    "full_response": (dict,),
    "model": (str,),
    "provider_id": (str,),
    "prompt_tokens": (int,),
    "completion_tokens": (int,),
    "total_request_tokens": (int,),
    "cost": (float,),
    "latency": (float,),
    "metadata": (dict,),
    "variables": (dict,),
    "tools": (list,),
    "tool_calls": (list,),
    "scores": (dict,),
    "response_format": (dict,),
    "limit_info": (dict,),
    "blurred": (bool,),
}

_TREE_FIELDS: Mapping[str, tuple[type, ...]] = {
    **_SPAN_FIELDS,
    "children": (list,),
}


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    candidate = value.strip()
    try:
        seconds = float(candidate)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(candidate)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (
            retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds
