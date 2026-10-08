"""Bounded, credential-free observations of the single declared control Space."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
import ssl
import threading
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request


ORIGIN = "https://szlholdings-szl-router-control.hf.space"
REPOSITORY = "szl-holdings/szl-router"
EXPECTED_REVISION = "e53dbb91cfe410cf50497c612e5b21ec9390211c"
PATHS = ("/api/source", "/readyz", "/readyz/inference")
MAX_BYTES = 16 * 1024
TIMEOUT = 8.0
CONTROLLED_FILES = {
    "router_control/app.py": "6c91808c7ec1c2ed3fff5de43117b9dc9b12ee71177913db8e32efaef5a62113",
    "router_control/local_store.py": "9e837fbdfa2b86a474312011c3a723a0a4d2befdd4b98a141d88d03b306f7143",
    "router_control/verification.py": "ccfc89f9e0f9a42a7a1927e94004f9b839c0f8ca50c7ef2b9ffbac8b5925e94b",
    "router_control/free_tier.py": "0adb7fbddc38055daa4c3001e937ebfdeb10be23f5295bea136e3fa710a78aaf",
    "router_control/static/index.html": "a72769c43f6ca53547ee565a024a873dae1656a7e04c6fc8a5744967fdc1e5f6",
    "router_control/static/app.js": "475b25080bf65983d56ee54b9677ff92586380144f95b9af27ef598b052b6ac6",
    "router_control/static/styles.css": "05d899ba38c3fd78874bb0af5a24f90e87a0e0d5b06e80814a1672db65d07d6d",
    "router_control/static/free-options.js": "7dcfad3abc321fe11a216ec3e1b81c20597ede9f895ddc1a9e1482befca49f41",
    "router_control/static/free-options.css": "826e95359c2b702c0e1b8a063e37908bd1f25e66ab5b627fcc9b9b69fc63add8",
}
CHECKS = ("registry_valid", "egress_enabled", "caller_auth_configured", "provider_configured")
MESSAGES = {
    "TIMEOUT": "The bounded cloud observation did not finish in time.",
    "NETWORK_ERROR": "The fixed cloud endpoint could not be read.",
    "TLS_ERROR": "The cloud TLS connection could not be verified.",
    "REDIRECT": "Cloud redirects are not followed.",
    "TOO_LARGE": "The cloud response exceeded the byte limit.",
    "INVALID_RESPONSE": "The cloud response was missing, malformed, or inconsistent.",
    "HTTP_STATUS": "The cloud endpoint returned an unexpected HTTP status.",
    "SOURCE_METADATA_MISMATCH": "The reported source metadata differs from the pinned source metadata.",
}


class ObservationError(Exception):
    def __init__(self, code: str):
        self.code = code if code in MESSAGES else "INVALID_RESPONSE"
        super().__init__(MESSAGES[self.code])


@dataclass(frozen=True)
class RemoteResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]
    url: str


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, message: str,
                         headers: Any, url: str) -> None:
        raise ObservationError("REDIRECT")


def _headers(headers: Any) -> dict[str, str]:
    result = {}
    for name in ("Content-Type", "Content-Length", "Content-Encoding"):
        values = headers.get_all(name, [])
        if len(values) > 1:
            raise ObservationError("INVALID_RESPONSE")
        if values:
            result[name.lower()] = values[0]
    return result


def _validate_envelope(response: RemoteResponse, path: str) -> None:
    if (not isinstance(response, RemoteResponse) or type(response.status) is not int
            or response.url != ORIGIN + path or not isinstance(response.body, bytes)):
        raise ObservationError("INVALID_RESPONSE")
    if 300 <= response.status < 400:
        raise ObservationError("REDIRECT")
    if len(response.body) > MAX_BYTES:
        raise ObservationError("TOO_LARGE")
    if not isinstance(response.headers, Mapping) or not all(isinstance(key, str) for key in response.headers):
        raise ObservationError("INVALID_RESPONSE")
    headers = {key.lower(): value for key, value in response.headers.items()}
    if len(headers) != len(response.headers) or not all(isinstance(value, str) for value in headers.values()):
        raise ObservationError("INVALID_RESPONSE")
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ObservationError("INVALID_RESPONSE")
    if headers.get("content-encoding", "identity").lower() != "identity":
        raise ObservationError("INVALID_RESPONSE")
    size = headers.get("content-length")
    if size is not None:
        if not re.fullmatch(r"[0-9]{1,8}", size):
            raise ObservationError("INVALID_RESPONSE")
        if int(size) > MAX_BYTES:
            raise ObservationError("TOO_LARGE")
        if int(size) != len(response.body):
            raise ObservationError("INVALID_RESPONSE")


def fetch(path: str, deadline: float) -> RemoteResponse:
    """GET only a fixed path, without ambient proxy/auth/cookie configuration."""
    if path not in PATHS:
        raise ObservationError("INVALID_RESPONSE")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ObservationError("TIMEOUT")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    request = urllib.request.Request(ORIGIN + path, method="GET", headers={
        "Accept": "application/json", "User-Agent": "SZL-Agents-Studio-Connection/1",
    })
    try:
        try:
            response = opener.open(request, timeout=remaining)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            headers = _headers(response.headers)
            size = headers.get("content-length")
            if size is not None and (not re.fullmatch(r"[0-9]{1,8}", size) or int(size) > MAX_BYTES):
                raise ObservationError("TOO_LARGE" if size.isdigit() else "INVALID_RESPONSE")
            chunks = []
            length = 0
            while True:
                if time.monotonic() >= deadline:
                    raise ObservationError("TIMEOUT")
                chunk = response.read1(min(4096, MAX_BYTES + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > MAX_BYTES:
                    raise ObservationError("TOO_LARGE")
            result = RemoteResponse(response.status, b"".join(chunks), headers, response.geturl())
            _validate_envelope(result, path)
            return result
    except ObservationError:
        raise
    except (TimeoutError,):
        raise ObservationError("TIMEOUT") from None
    except ssl.SSLError:
        raise ObservationError("TLS_ERROR") from None
    except urllib.error.URLError as exc:
        code = "TLS_ERROR" if isinstance(exc.reason, ssl.SSLError) else "NETWORK_ERROR"
        raise ObservationError(code) from None
    except (OSError, ValueError):
        raise ObservationError("NETWORK_ERROR") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite number")
    return result


def _constant(value: str) -> Any:
    raise ValueError("nonfinite constant")


def _decode(response: RemoteResponse, path: str) -> dict[str, Any]:
    _validate_envelope(response, path)
    try:
        value = json.loads(response.body.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_float=_finite_float, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ObservationError("INVALID_RESPONSE") from None
    if not isinstance(value, dict):
        raise ObservationError("INVALID_RESPONSE")
    return value


def _blank(observed_at: str) -> dict[str, Any]:
    return {
        "schema": "szl.studio.cloud-connection/v1", "evidence_class": "MEASURED",
        "observed_at": observed_at, "origin": ORIGIN,
        "source": {"repository": None, "revision": None, "expected_revision": EXPECTED_REVISION,
                   "matches": None, "controlled_files_match": None, "state": "UNKNOWN", "http_status": None},
        "control": {"state": "UNKNOWN", "http_status": None},
        "inference": {"state": "UNKNOWN", "http_status": None, "checks": dict.fromkeys(CHECKS),
                      "ready_for_requests": None, "provider_reachability": "UNKNOWN",
                      "inference_witness": "UNKNOWN", "basis": "UNKNOWN"},
        "errors": [],
    }


def _error(result: dict[str, Any], path: str, code: str) -> None:
    result["errors"].append({"endpoint": path, "code": code, "message": MESSAGES[code]})


def _source(data: dict[str, Any]) -> dict[str, Any]:
    revision = data.get("revision")
    files = data.get("controlled_files")
    receipt = data.get("receipt")
    if (data.get("schema") != "szl.router-source/v1" or data.get("repository") != REPOSITORY
            or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or not isinstance(files, dict) or set(files) != set(CONTROLLED_FILES)
            or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in files.values())
            or any(data.get(key) is not False for key in ("default_egress", "secret_output", "arbitrary_url_routing"))
            or not isinstance(receipt, dict) or receipt.get("algorithm") != "sha256"
            or not isinstance(receipt.get("digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt["digest"])):
        raise ObservationError("INVALID_RESPONSE")
    matches = revision == EXPECTED_REVISION
    files_match = files == CONTROLLED_FILES
    return {"repository": REPOSITORY, "revision": revision, "expected_revision": EXPECTED_REVISION,
            "matches": matches, "controlled_files_match": files_match,
            "state": "MATCH" if matches and files_match else "MISMATCH"}


def _control(data: dict[str, Any], status: int) -> dict[str, Any]:
    checks = data.get("checks")
    if (data.get("scope") != "CONTROL_PLANE" or data.get("status") not in ("ready", "unavailable")
            or not isinstance(checks, dict) or set(checks) != {"static_index", "registry", "egress"}
            or not all(type(value) is bool for value in checks.values())
            or type(data.get("egress_enabled")) is not bool
            or not isinstance(data.get("registry_state"), str)):
        raise ObservationError("INVALID_RESPONSE")
    ready = data["status"] == "ready"
    if ready != all(checks.values()) or status != (200 if ready else 503):
        raise ObservationError("INVALID_RESPONSE")
    return {"state": "READY" if ready else "UNAVAILABLE"}


def _inference(data: dict[str, Any], status: int) -> dict[str, Any]:
    checks = data.get("checks")
    if (data.get("status") not in ("ready", "unavailable") or not isinstance(checks, dict)
            or set(checks) != set(CHECKS) or not all(type(value) is bool for value in checks.values())
            or type(data.get("ready_for_requests")) is not bool
            or type(data.get("remote_credentialed_provider")) is not bool
            or data.get("basis") != "LOCAL_CONFIGURATION_ONLY"
            or data.get("provider_reachability") != "UNVERIFIED"
            or data.get("inference_witness") != "UNAVAILABLE"
            or data.get("model_identity") != "UNVERIFIED"):
        raise ObservationError("INVALID_RESPONSE")
    ready = all(checks.values())
    if (data["ready_for_requests"] != ready or (data["status"] == "ready") != ready
            or status != (200 if ready else 503)):
        raise ObservationError("INVALID_RESPONSE")
    return {"state": "READY" if ready else "UNAVAILABLE", "checks": checks,
            "ready_for_requests": ready, "provider_reachability": "UNVERIFIED",
            "inference_witness": "UNAVAILABLE", "basis": "LOCAL_CONFIGURATION_ONLY"}


def parse_observation(source_response: RemoteResponse | ObservationError,
                      control_response: RemoteResponse | ObservationError,
                      inference_response: RemoteResponse | ObservationError,
                      observed_at: str) -> dict[str, Any]:
    result = _blank(observed_at)
    for path, name, response in zip(PATHS, ("source", "control", "inference"),
                                    (source_response, control_response, inference_response)):
        try:
            if isinstance(response, ObservationError):
                if response.code in ("TIMEOUT", "NETWORK_ERROR", "TLS_ERROR"):
                    result[name]["state"] = "UNAVAILABLE" if name != "source" else "UNKNOWN"
                raise response
            _validate_envelope(response, path)
            result[name]["http_status"] = response.status
            data = _decode(response, path)
            if response.status not in ((200,) if name == "source" else (200, 503)):
                result[name]["state"] = "UNAVAILABLE" if name != "source" else "UNKNOWN"
                raise ObservationError("HTTP_STATUS")
            parsed = _source(data) if name == "source" else (
                _control(data, response.status) if name == "control" else _inference(data, response.status))
            result[name].update(parsed)
            if name == "source" and parsed["state"] == "MISMATCH":
                _error(result, path, "SOURCE_METADATA_MISMATCH")
        except ObservationError as exc:
            _error(result, path, exc.code)
    return result


@dataclass
class _Cycle:
    deadline: float
    observed_at: str
    done: threading.Event
    result: dict[str, Any] | None = None
    finished: float | None = None


class CloudConnection:
    def __init__(self, fetcher: Callable[[str, float], RemoteResponse] | None = None,
                 timeout: float = TIMEOUT):
        if not 0 < timeout <= TIMEOUT:
            raise ValueError("Cloud timeout must be positive and bounded.")
        self.fetcher = fetcher or fetch
        self.timeout = timeout
        self.lock = threading.Lock()
        self.cycle: _Cycle | None = None

    def _observe(self, cycle: _Cycle) -> None:
        responses = []
        try:
            for path in PATHS:
                try:
                    if time.monotonic() >= cycle.deadline:
                        raise ObservationError("TIMEOUT")
                    responses.append(self.fetcher(path, cycle.deadline))
                except ObservationError as exc:
                    responses.append(exc)
                except Exception:
                    responses.append(ObservationError("NETWORK_ERROR"))
            cycle.result = parse_observation(*responses, cycle.observed_at)
        finally:
            cycle.finished = time.monotonic()
            cycle.done.set()

    def check(self) -> dict[str, Any]:
        # A single lingering DNS/read worker cannot multiply on repeated local requests.
        with self.lock:
            if self.cycle is None or self.cycle.done.is_set():
                self.cycle = _Cycle(time.monotonic() + self.timeout,
                                    datetime.now(timezone.utc).isoformat(), threading.Event())
                threading.Thread(target=self._observe, args=(self.cycle,), daemon=True).start()
            cycle = self.cycle
        if (cycle.done.wait(max(0.0, cycle.deadline - time.monotonic())) and cycle.result is not None
                and cycle.finished is not None and cycle.finished <= cycle.deadline):
            return cycle.result
        timeout = ObservationError("TIMEOUT")
        return parse_observation(timeout, timeout, timeout, cycle.observed_at)
