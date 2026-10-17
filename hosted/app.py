"""Public-account application, isolated from the Windows loopback Studio."""

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from authlib.integrations.flask_client import OAuth
from authlib.integrations.requests_client import OAuth2Session
from flask import Flask, jsonify, redirect, request, session
from werkzeug.exceptions import HTTPException

from engine import Engine, MAX_OUTPUT_TOKENS

ISSUER = "https://huggingface.co"
SESSION_SECONDS = 8 * 60 * 60
MAX_BODY_BYTES = 16384
MAX_STORE_RESPONSE_BYTES = 2 * 1024 * 1024
CONNECT_BUDGET_SECONDS = 8
ADDRESS_BUDGET_SECONDS = 3
IPV6_BUDGET_SECONDS = 1
READ_BUDGET_SECONDS = 15
# The Space completes the small TCP handshake and then waits out the address
# budget with TimeoutError, so no certificate is received to verify. Announce
# this MSS before connect so the peer's larger handshake flight stays segmented.
TCP_SEGMENT_CEILING = 1200
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
LIMITS = {"prompt_characters": 2000, "output_tokens": MAX_OUTPUT_TOKENS,
          "daily_user_runs": 20, "daily_global_runs": 200, "conversations": 100}


class ApiError(Exception):
    def __init__(self, code, status=400, reason=None, submitted=False):
        super().__init__(code)
        self.code = code
        self.status = status
        self.reason = reason
        self.submitted = submitted


class StoreFailure(Exception):
    def __init__(self, reason, submitted, phase, elapsed_ms, error_name):
        super().__init__(reason)
        self.reason = reason
        self.submitted = submitted
        self.phase = phase
        self.elapsed_ms = elapsed_ms
        self.error_name = error_name


class ProviderSession(OAuth2Session):
    """Keep the maintained OAuth protocol implementation on the one provider."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.trust_env = False

    def request(self, method, url, *args, **kwargs):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "huggingface.co"
                or parsed.username or parsed.password):
            raise ValueError("invalid_identity_endpoint")
        kwargs["timeout"] = (3.05, 15)
        kwargs["allow_redirects"] = False
        return super().request(method, url, *args, **kwargs)


def valid_origin(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return bool(parsed.scheme == "https" and parsed.hostname
                and parsed.netloc == parsed.hostname and not parsed.path
                and not parsed.query and not parsed.fragment
                and not parsed.username and not parsed.password)


def valid_store_url(value):
    if not valid_origin(value):
        return False
    host = urlsplit(value).hostname
    return bool(re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", host))


def decode_store_response(raw):
    if not isinstance(raw, (bytes, bytearray)) or b"\r\n\r\n" not in raw:
        raise ApiError("store_unavailable", 503, reason="invalid_response")
    head, body = raw.split(b"\r\n\r\n", 1)
    if len(head) > 65536 or len(body) > MAX_STORE_RESPONSE_BYTES:
        raise ApiError("store_unavailable", 503, reason="invalid_response")
    lines = head.decode("iso-8859-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ApiError("store_unavailable", 503, reason="invalid_response")
    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            raise ApiError("store_unavailable", 503, reason="invalid_response")
        name, value = line.split(":", 1)
        headers.setdefault(name.strip().lower(), value.strip())
    if headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ApiError("store_unavailable", 503, reason="invalid_response")
    try:
        payload = json.loads(body)
    except ValueError:
        raise ApiError("store_unavailable", 503, reason="invalid_response") from None
    if not isinstance(payload, dict):
        raise ApiError("store_unavailable", 503, reason="invalid_response")
    status = int(parts[1])
    if status != 200:
        aliases = {"not_found": "not_found",
                   "idempotency_conflict": "idempotency_conflict",
                   "conversation_unresolved": "conversation_pending",
                   "conversation_limit": "quota_exceeded",
                   "conversation_run_limit": "quota_exceeded",
                   "user_daily_limit": "quota_exceeded",
                   "global_daily_limit": "quota_exceeded",
                   "context_changed": "context_changed"}
        if status not in {400, 404, 409, 413, 429}:
            raise ApiError("store_unavailable", 503, reason="invalid_response")
        code = aliases.get(payload.get("error"))
        if code is None:
            raise ApiError("store_unavailable", 503, reason="invalid_response")
        raise ApiError(code, status)
    return payload


class Store:
    def __init__(self, url, secret, *, connect_budget=CONNECT_BUDGET_SECONDS,
                 address_budget=ADDRESS_BUDGET_SECONDS, read_budget=READ_BUDGET_SECONDS):
        self.url = url
        self.secret = secret
        self.connect_budget = connect_budget
        self.address_budget = address_budget
        self.read_budget = read_budget
        self._clock = time.monotonic
        self.configured = (valid_store_url(url) and isinstance(secret, str)
                           and bool(re.fullmatch(r"[\x21-\x7e]{32,256}", secret)))

    def call(self, op, **data):
        if not self.configured:
            raise ApiError("store_unavailable", 503, reason="missing_configuration")
        correlation = secrets.token_hex(4)
        self._emit(correlation, phase="config", result="direct", proxy_env=self._proxy_env(),
                   trust_env="false", direct="true", **self._pressure())
        # IPv6 on this Space fails before TCP. IPv4 can accept TCP and then stall
        # in TLS. urllib3 reports that stall as ReadTimeout and does not try the
        # next address. A response timeout can also follow a committed write.
        # Another address is used only before the request bytes are written.
        deadline = self._clock() + self.connect_budget
        last = None
        for family in (socket.AF_INET6, socket.AF_INET):
            if self._clock() >= deadline:
                break
            # This Space's IPv6 path fails before TCP. Keep that attempt inside
            # the old 1s cap so it cannot consume the IPv4 connect budget.
            family_deadline = deadline
            if family == socket.AF_INET6:
                family_deadline = min(deadline, self._clock() + IPV6_BUDGET_SECONDS)
            try:
                return self._exchange(op, data, family, family_deadline, correlation)
            except StoreFailure as failure:
                if failure.submitted:
                    raise ApiError("store_unavailable", 503, reason=failure.reason,
                                   submitted=True) from None
                last = failure
        reason = last.reason if last is not None else "connect_failure"
        raise ApiError("store_unavailable", 503, reason=reason)

    def _exchange(self, op, data, family, deadline, correlation):
        host = urlsplit(self.url).hostname
        started = self._clock()
        try:
            infos = socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM)
        except socket.gaierror as exc:
            failure = StoreFailure("dns_failure", False, "dns", self._elapsed(started),
                                   type(exc).__name__)
            self._emit(correlation, family=self._family_name(family), phase="dns",
                       ms=failure.elapsed_ms, result=failure.reason, error=failure.error_name)
            raise failure from None
        self._emit(correlation, family=self._family_name(family), phase="dns",
                   ms=self._elapsed(started), addresses=len(infos), result="ok")
        if not infos:
            failure = StoreFailure("dns_failure", False, "dns", self._elapsed(started), "EmptyResult")
            self._emit(correlation, family=self._family_name(family), phase="dns",
                       ms=failure.elapsed_ms, result=failure.reason, error=failure.error_name)
            raise failure
        last = None
        for index, info in enumerate(infos):
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            outcome = self._one_address(op, data, host, family, info, index, remaining, correlation)
            if not isinstance(outcome, StoreFailure):
                return outcome
            if outcome.submitted:
                raise outcome
            last = outcome
        if last is not None:
            raise last
        failure = StoreFailure("connect_failure", False, "budget", self._elapsed(started), "TimeoutError")
        self._emit(correlation, family=self._family_name(family), phase="budget",
                   ms=failure.elapsed_ms, result=failure.reason, error=failure.error_name)
        raise failure

    def _limit_segment(self, sock):
        option = getattr(socket, "TCP_MAXSEG", None)
        if option is None:
            return "unsupported"
        try:
            sock.setsockopt(socket.IPPROTO_TCP, option, TCP_SEGMENT_CEILING)
            actual = sock.getsockopt(socket.IPPROTO_TCP, option)
        except OSError:
            return "unsupported"
        if isinstance(actual, int) and 0 < actual <= TCP_SEGMENT_CEILING:
            return str(actual)
        return "unsupported"

    def _one_address(self, op, data, host, family, info, index, remaining, correlation):
        af, socktype, proto, _canon, sockaddr = info
        address_id = f"{index}-" + hashlib.sha256(str(sockaddr[0]).encode()).hexdigest()[:8]
        budget = min(self.address_budget, remaining)
        started = self._clock()
        sock = socket.socket(af, socktype, proto)
        closer = sock
        try:
            segment = self._limit_segment(sock)
            sock.settimeout(budget)
            try:
                sock.connect(sockaddr)
            except TimeoutError as exc:
                raise StoreFailure("connect_failure", False, "tcp", self._elapsed(started),
                                   type(exc).__name__) from None
            except OSError as exc:
                raise StoreFailure("connect_failure", False, "tcp", self._elapsed(started),
                                   type(exc).__name__) from None
            self._emit(correlation, family=self._family_name(family), phase="tcp",
                       ms=self._elapsed(started), address=address_id, segment=segment,
                       result="connected")
            tls_started = self._clock()
            tls_budget = (started + budget) - tls_started
            if tls_budget <= 0:
                raise StoreFailure("tls_failure", False, "tls", 0, "TimeoutError")
            sock.settimeout(tls_budget)
            try:
                context = ssl.create_default_context()
                closer = context.wrap_socket(sock, server_hostname=host)
            except TimeoutError as exc:
                raise StoreFailure("tls_failure", False, "tls", self._elapsed(tls_started),
                                   type(exc).__name__) from None
            except (OSError, ssl.SSLError) as exc:
                raise StoreFailure("tls_failure", False, "tls", self._elapsed(tls_started),
                                   type(exc).__name__) from None
            self._emit(correlation, family=self._family_name(family), phase="tls",
                       ms=self._elapsed(tls_started), address=address_id, result="verified")
            return self._submit(closer, op, data, correlation, family, address_id)
        except StoreFailure as failure:
            self._emit(correlation, family=self._family_name(family), phase=failure.phase,
                       ms=failure.elapsed_ms, address=address_id, result=failure.reason,
                       error=failure.error_name)
            return failure
        finally:
            closer.close()

    def _submit(self, tls, op, data, correlation, family, address_id):
        try:
            payload = json.dumps({"op": op, **data}).encode()
        except (TypeError, ValueError) as exc:
            raise StoreFailure("invalid_response", False, "encode", 0, type(exc).__name__) from None
        if len(payload) > MAX_BODY_BYTES:
            raise StoreFailure("invalid_response", False, "encode", 0, "BodyTooLarge")
        host = urlsplit(self.url).hostname
        message = (
            f"POST /v1/store HTTP/1.1\r\nHost: {host}\r\n"
            f"Authorization: Bearer {self.secret}\r\nAccept: application/json\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode() + payload
        # Bytes may leave this process as soon as send starts. A later timeout
        # is not proof that the store failed to commit, so this attempt ends.
        started = self._clock()
        try:
            tls.settimeout(self.read_budget)
            tls.sendall(message)
            raw = self._read_response(tls)
        except TimeoutError as exc:
            raise StoreFailure("response_timeout", True, "response", self._elapsed(started),
                               type(exc).__name__) from None
        except OSError as exc:
            raise StoreFailure("response_timeout", True, "response", self._elapsed(started),
                               type(exc).__name__) from None
        self._emit(correlation, family=self._family_name(family), phase="response",
                   ms=self._elapsed(started), address=address_id, result="ok")
        try:
            return decode_store_response(raw)
        except ApiError as exc:
            # A body arrived after the request was written. An unusable body does
            # not prove the write failed, so the caller must not send it again.
            if exc.reason == "invalid_response":
                exc.submitted = True
            raise

    def _read_response(self, tls):
        # Stop at Content-Length. Waiting for the server to close would turn a
        # finished response into a response timeout and could replay nothing
        # useful while hiding a body that already arrived.
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            block = tls.recv(16384)
            if not block:
                break
            buffer.extend(block)
            if len(buffer) > MAX_STORE_RESPONSE_BYTES + 65536:
                raise StoreFailure("invalid_response", True, "response", 0, "Oversized")
        if b"\r\n\r\n" not in buffer:
            # The request was already written. No header means the outcome is
            # unknown, so a purge must stay uncertain instead of being retried.
            if not buffer:
                raise StoreFailure("response_timeout", True, "response", 0, "Closed")
            raise StoreFailure("invalid_response", True, "response", 0, "Truncated")
        head, rest = buffer.split(b"\r\n\r\n", 1)
        body = bytearray(rest)
        headers = {}
        for line in head.split(b"\r\n")[1:]:
            if b":" not in line:
                continue
            name, value = line.split(b":", 1)
            key = bytes(name).strip().lower()
            if key in headers and key in {b"content-length", b"transfer-encoding"}:
                raise StoreFailure("invalid_response", True, "response", 0, "DuplicateHeader")
            headers.setdefault(key, bytes(value).strip().lower())
        chunked = b"chunked" in headers.get(b"transfer-encoding", b"")
        if chunked:
            body = self._read_chunked(tls, body)
        elif b"content-length" in headers:
            try:
                length = int(headers[b"content-length"])
            except ValueError:
                raise StoreFailure("invalid_response", True, "response", 0, "ContentLength") from None
            if length < 0 or length > MAX_STORE_RESPONSE_BYTES:
                raise StoreFailure("invalid_response", True, "response", 0, "ContentLength")
            while len(body) < length:
                block = tls.recv(16384)
                if not block:
                    raise StoreFailure("invalid_response", True, "response", 0, "Truncated")
                body.extend(block)
            if len(body) < length:
                raise StoreFailure("invalid_response", True, "response", 0, "Truncated")
            body = body[:length]
        else:
            while len(body) <= MAX_STORE_RESPONSE_BYTES:
                block = tls.recv(16384)
                if not block:
                    break
                body.extend(block)
        return bytes(head + b"\r\n\r\n" + body)

    def _read_chunked(self, tls, pending):
        output = bytearray()
        pending = bytearray(pending)
        while True:
            while b"\r\n" not in pending:
                block = tls.recv(16384)
                if not block:
                    raise StoreFailure("invalid_response", True, "response", 0, "Truncated")
                pending.extend(block)
            line, rest = pending.split(b"\r\n", 1)
            pending = bytearray(rest)
            try:
                size = int(line.split(b";", 1)[0].strip() or b"zz", 16)
            except ValueError:
                raise StoreFailure("invalid_response", True, "response", 0, "ChunkSize") from None
            if size == 0:
                return output
            if size > MAX_STORE_RESPONSE_BYTES or len(output) + size > MAX_STORE_RESPONSE_BYTES:
                raise StoreFailure("invalid_response", True, "response", 0, "Oversized")
            while len(pending) < size + 2:
                block = tls.recv(16384)
                if not block:
                    raise StoreFailure("invalid_response", True, "response", 0, "Truncated")
                pending.extend(block)
            output.extend(pending[:size])
            if pending[size:size + 2] != b"\r\n":
                raise StoreFailure("invalid_response", True, "response", 0, "ChunkFrame")
            pending = bytearray(pending[size + 2:])

    def _elapsed(self, started):
        return int((self._clock() - started) * 1000)

    def _family_name(self, family):
        if family == socket.AF_INET6:
            return "ipv6"
        if family == socket.AF_INET:
            return "ipv4"
        return "other"

    def _proxy_env(self):
        names = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                 "https_proxy", "http_proxy", "all_proxy", "no_proxy")
        present = [name for name in names if os.environ.get(name)]
        return ",".join(present) if present else "none"

    def _pressure(self):
        rss = threads = "unavailable"
        try:
            status = Path("/proc/self/status").read_text(encoding="utf-8")
        except OSError:
            status = ""
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                rss = line.split()[1]
            elif line.startswith("Threads:"):
                threads = line.split()[1]
        return {"rss_kb": rss, "proc_threads": threads,
                "python_threads": threading.active_count()}

    def _emit(self, correlation, **fields):
        order = ("family", "phase", "ms", "addresses", "address", "segment", "result", "error",
                 "proxy_env", "trust_env", "direct", "rss_kb", "proc_threads", "python_threads")
        parts = [f"store_transport correlation={correlation}"]
        for key in order:
            if key in fields and fields[key] is not None:
                parts.append(f"{key}={fields[key]}")
        print(" ".join(parts), file=sys.stderr, flush=True)


def owner_key(subject):
    return hashlib.sha256(json.dumps([ISSUER, subject], separators=(",", ":")).encode()).hexdigest()


def create_app(config=None, *, store=None, engine=None, oauth_client=None):
    app = Flask(__name__, static_url_path="/static")
    app.config.update(
        PUBLIC_ORIGIN=os.getenv("PUBLIC_ORIGIN", ""),
        SESSION_SECRET=os.getenv("SESSION_SECRET", ""),
        OAUTH_CLIENT_ID=os.getenv("OAUTH_CLIENT_ID", ""),
        OAUTH_CLIENT_SECRET=os.getenv("OAUTH_CLIENT_SECRET", ""),
        OPENID_PROVIDER_URL=os.getenv("OPENID_PROVIDER_URL", ""),
        STORE_URL=os.getenv("STORE_URL", ""), STORE_SECRET=os.getenv("STORE_SECRET", ""),
        SESSION_COOKIE_NAME="__Host-studio-session", SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH="/", SESSION_REFRESH_EACH_REQUEST=False,
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_SECONDS),
        MAX_CONTENT_LENGTH=MAX_BODY_BYTES, MAX_FORM_MEMORY_SIZE=MAX_BODY_BYTES,
        MAX_FORM_PARTS=8,
    )
    if config:
        app.config.update(config)
    secret = app.config["SESSION_SECRET"]
    secret_ready = isinstance(secret, str) and len(secret) >= 32
    app.secret_key = secret if secret_ready else secrets.token_hex(32)
    origin = app.config["PUBLIC_ORIGIN"]
    auth_ready = bool(valid_origin(origin) and secret_ready
                      and app.config["OAUTH_CLIENT_ID"] and app.config["OAUTH_CLIENT_SECRET"]
                      and app.config["OPENID_PROVIDER_URL"] == ISSUER)
    store = store if store is not None else Store(app.config["STORE_URL"], app.config["STORE_SECRET"])
    engine = engine if engine is not None else Engine()
    oauth = OAuth(app)
    if oauth_client is None and auth_ready:
        oauth_client = oauth.register(
            name="hf", client_id=app.config["OAUTH_CLIENT_ID"],
            client_secret=app.config["OAUTH_CLIENT_SECRET"],
            server_metadata_url=ISSUER + "/.well-known/openid-configuration",
            client_kwargs={"scope": "openid profile", "code_challenge_method": "S256"},
        )
        oauth_client.client_cls = ProviderSession
    app.extensions["studio_store"] = store
    app.extensions["studio_engine"] = engine
    app.extensions["studio_oauth_client"] = oauth_client

    def current_user():
        user = session.get("user")
        if (not auth_ready or not isinstance(user, dict)
                or not isinstance(user.get("owner"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", user["owner"])
                or not isinstance(user.get("authenticated_at"), (int, float))
                or not 0 <= time.time() - user["authenticated_at"] < SESSION_SECONDS):
            return None
        return user

    def require_user():
        user = current_user()
        if user is None:
            raise ApiError("authentication_required", 401)
        return user

    def store_diagnostic():
        try:
            result = store.call("check")
        except ApiError as exc:
            return {"ok": False, "failure": exc.reason or "connect_failure"}
        schema_version = result.get("schema_version")
        matched = (result.get("status") == "available" and type(schema_version) is int
                   and schema_version == 1 and result.get("receipt_minted") is False)
        if matched:
            return {"ok": True, "failure": None}
        return {"ok": False, "failure": "schema_mismatch"}

    def csrf_check(form=False):
        if not valid_origin(origin) or request.headers.get("Origin") != origin:
            raise ApiError("origin_rejected", 403)
        token = request.form.get("csrf") if form else request.headers.get("X-CSRF-Token")
        expected = session.get("csrf")
        if (not isinstance(token, str) or not isinstance(expected, str)
                or not hmac.compare_digest(token.encode(), expected.encode())):
            raise ApiError("csrf_rejected", 403)

    def body(fields):
        csrf_check()
        if not request.is_json:
            raise ApiError("json_required", 415)
        value = request.get_json()
        if not isinstance(value, dict) or set(value) != set(fields):
            raise ApiError("invalid_request")
        return value

    def text(value, maximum):
        if (not isinstance(value, str) or not value.strip() or len(value) > maximum
                or "\0" in value or any(0xd800 <= ord(c) <= 0xdfff for c in value)):
            raise ApiError("invalid_request")
        return value

    def identifier(value):
        if not isinstance(value, str) or not UUID_PATTERN.fullmatch(value):
            raise ApiError("invalid_request")
        return value

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; "
            "base-uri 'none'; form-action 'self' https://huggingface.co"
        )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.errorhandler(ApiError)
    def api_error(error):
        return jsonify(error=error.code), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error="request_too_large" if error.code == 413 else "invalid_request"), error.code

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/healthz")
    def health():
        return jsonify(status="available", receipt_minted=False)

    @app.get("/api/build-info")
    def build_info():
        try:
            source = json.loads(Path(__file__).with_name("source.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            source = {"source_revision": "UNKNOWN"}
        return jsonify(source=source, model=engine.identity(), receipt_minted=False)

    @app.get("/readyz")
    def ready():
        diagnostic = store_diagnostic()
        result = {"auth": auth_ready, "store": diagnostic["ok"], "model": bool(engine.ready)}
        body = {"ready": result, "receipt_minted": False}
        if diagnostic["failure"]:
            body["store_failure"] = diagnostic["failure"]
        return jsonify(body), 200 if all(result.values()) else 503

    @app.get("/api/session")
    def session_info():
        # Only browser session state is initialized; no store writes or receipts.
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        user = current_user()
        diagnostic = store_diagnostic()
        body = {"user": {"name": user["name"]} if user else None,
                "csrf": session["csrf"], "limits": LIMITS,
                "ready": {"auth": auth_ready, "store": diagnostic["ok"],
                          "model": bool(engine.ready)}}
        if diagnostic["failure"]:
            body["store_failure"] = diagnostic["failure"]
        return jsonify(body)

    @app.post("/auth/login")
    def login():
        csrf_check(form=True)
        if not auth_ready:
            raise ApiError("authentication_unavailable", 503)
        last_attempt = session.get("login_started", 0)
        if isinstance(last_attempt, (int, float)) and time.time() - last_attempt < 10:
            raise ApiError("login_rate_limited", 429)
        session.clear()
        session["login_started"] = time.time()
        try:
            return oauth_client.authorize_redirect(origin + "/auth/callback")
        except Exception:
            session.clear()
            raise ApiError("authentication_unavailable", 503) from None

    @app.get("/auth/callback")
    def callback():
        if not auth_ready:
            raise ApiError("authentication_unavailable", 503)
        try:
            token = oauth_client.authorize_access_token()
            # Authlib verifies the ID token signature, issuer, audience, nonce,
            # expiry and authorization state before exposing this userinfo.
            user = token.get("userinfo")
            if (not isinstance(user, dict) or user.get("iss") != ISSUER
                    or not isinstance(user.get("sub"), str) or not 0 < len(user["sub"]) <= 200):
                raise ValueError("invalid_identity")
            name = user.get("preferred_username") or user.get("name") or "Hugging Face user"
            if not isinstance(name, str) or not 0 < len(name) <= 120:
                name = "Hugging Face user"
            session.clear()
            session.permanent = True
            session["csrf"] = secrets.token_urlsafe(32)
            session["user"] = {"owner": owner_key(user["sub"]), "name": name,
                               "authenticated_at": time.time()}
            # OAuth tokens intentionally remain in this request only.
            return redirect(origin + "/", code=303)
        except Exception:
            session.clear()
            raise ApiError("authentication_failed", 401) from None

    @app.post("/auth/logout")
    def logout():
        body(set())
        session.clear()
        return jsonify(status="signed_out")

    @app.delete("/api/account")
    def delete_account():
        user = require_user()
        body(set())
        try:
            result = store.call("purge", owner=user["owner"])
        except ApiError as exc:
            # response_timeout is the route's signal for a lost purge body.
            # submitted covers every other post-send failure, including a
            # truncated body. A failure before send can be retried by the user.
            if exc.submitted or exc.reason == "response_timeout":
                raise ApiError("erasure_uncertain", 503) from None
            raise
        if result.get("status") not in {"deleted", "replay"} or not isinstance(result.get("deleted"), dict):
            raise ApiError("store_unavailable", 503)
        session.clear()
        return jsonify(status="deleted", deleted=result["deleted"], receipt=result.get("receipt"))

    @app.get("/api/conversations")
    def conversations():
        user = require_user()
        return jsonify(store.call("list", owner=user["owner"]))

    @app.post("/api/conversations")
    def create_conversation():
        user = require_user()
        data = body({"title"})
        return jsonify(store.call("create", owner=user["owner"], id=str(uuid.uuid4()),
                                  title=text(data["title"], 120))), 201

    @app.get("/api/conversations/<conversation_id>")
    def conversation(conversation_id):
        user = require_user()
        return jsonify(store.call("get", owner=user["owner"], id=identifier(conversation_id)))

    @app.post("/api/runs")
    def run():
        user = require_user()
        data = body({"conversation_id", "prompt", "idempotency_key"})
        conversation_id = identifier(data["conversation_id"])
        key = identifier(data["idempotency_key"])
        prompt = text(data["prompt"], LIMITS["prompt_characters"])
        previous = store.call("get", owner=user["owner"], id=conversation_id)
        conversation = previous.get("conversation")
        if (not isinstance(conversation, dict) or conversation.get("owner") != user["owner"]
                or conversation.get("id") != conversation_id):
            raise ApiError("store_unavailable", 503)
        messages = previous.get("messages")
        runs = previous.get("runs")
        if not isinstance(messages, list) or not isinstance(runs, list):
            raise ApiError("store_unavailable", 503)
        for saved in runs:
            if (not isinstance(saved, dict) or saved.get("owner") != user["owner"]
                    or saved.get("conversation_id") != conversation_id):
                raise ApiError("store_unavailable", 503)
            if saved.get("idempotency_key") != key:
                continue
            if (not isinstance(saved.get("id"), str)
                    or not UUID_PATTERN.fullmatch(saved["id"])
                    or saved.get("state") not in {"pending", "completed", "uncertain"}):
                raise ApiError("store_unavailable", 503)
            if saved.get("prompt") != prompt or saved.get("max_output_tokens") != MAX_OUTPUT_TOKENS:
                raise ApiError("idempotency_conflict", 409)
            return jsonify(status=saved["state"], run=saved, replay=True)
        if len(messages) >= 20:
            raise ApiError("context_limit", 400)
        history = []
        try:
            history = [{"role": item["role"], "content": item["content"]} for item in messages]
        except (TypeError, KeyError):
            raise ApiError("store_unavailable", 503) from None
        try:
            with engine.admission(history + [{"role": "user", "content": prompt}]) as generate:
                reservation = store.call("reserve", owner=user["owner"], conversation_id=conversation_id,
                                         idempotency_key=key, prompt=prompt,
                                         max_output_tokens=MAX_OUTPUT_TOKENS,
                                         expected_message_count=len(messages))
                saved = reservation.get("run")
                if (not isinstance(saved, dict) or saved.get("owner") != user["owner"]
                        or saved.get("conversation_id") != conversation_id
                        or saved.get("idempotency_key") != key
                        or saved.get("prompt") != prompt
                        or saved.get("max_output_tokens") != MAX_OUTPUT_TOKENS
                        or not isinstance(saved.get("id"), str)
                        or not UUID_PATTERN.fullmatch(saved["id"])):
                    raise ApiError("store_unavailable", 503)
                if reservation.get("status") == "replay":
                    if (saved.get("state") not in {"pending", "completed", "uncertain"}
                            or saved.get("prompt") != prompt
                            or saved.get("max_output_tokens") != MAX_OUTPUT_TOKENS):
                        raise ApiError("store_unavailable", 503)
                    return jsonify(status=saved["state"], run=saved, replay=True)
                if reservation.get("status") != "pending" or saved.get("state") != "pending":
                    raise ApiError("store_unavailable", 503)
                try:
                    generated = generate()
                    completed = store.call("complete", owner=user["owner"], run_id=saved["id"],
                                           output=generated["text"], output_tokens=generated["output_tokens"],
                                           provider_response_id=generated["provider_response_id"])
                    if (completed.get("run", {}).get("state") != "completed"
                            or completed["run"].get("owner") != user["owner"]
                            or completed["run"].get("id") != saved["id"]):
                        raise ApiError("store_unavailable", 503)
                    return jsonify(status="completed", run=completed["run"], receipt=completed.get("receipt"))
                except Exception:
                    # A lost completion response may still have committed. Read it
                    # back before any other mutation. Never resend generation, and
                    # never mark a committed completion uncertain.
                    observed = None
                    try:
                        snapshot = store.call("get", owner=user["owner"], id=conversation_id)
                        runs = snapshot.get("runs") if isinstance(snapshot, dict) else None
                        if isinstance(runs, list):
                            observed = next((item for item in runs
                                             if isinstance(item, dict)
                                             and item.get("id") == saved["id"]
                                             and item.get("owner") == user["owner"]), None)
                    except Exception:
                        observed = None
                    if (isinstance(observed, dict) and observed.get("state") == "completed"
                            and isinstance(observed.get("output"), str)):
                        return jsonify(status="completed", run=observed, receipt=None)
                    if isinstance(observed, dict) and observed.get("state") == "pending":
                        try:
                            store.call("uncertain", owner=user["owner"], run_id=saved["id"])
                        except Exception:
                            pass
                    return jsonify(status="uncertain", run_id=saved["id"],
                                   error="run_outcome_uncertain"), 503
        except ValueError as error:
            if str(error) == "context_limit":
                raise ApiError("context_limit", 400) from None
            raise ApiError("invalid_request", 400) from None
        except RuntimeError as error:
            if str(error) == "model_busy":
                raise ApiError("model_busy", 429) from None
            if str(error) == "model_unavailable":
                raise ApiError("model_unavailable", 503) from None
            raise ApiError("model_unavailable", 503) from None

    return app
