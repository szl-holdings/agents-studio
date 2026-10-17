"""Public-account application, isolated from the Windows loopback Studio."""

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import traceback
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests
from urllib3.util import connection as urllib3_connection
from urllib3.exceptions import MaxRetryError, NewConnectionError
from authlib.integrations.flask_client import OAuth
from authlib.integrations.requests_client import OAuth2Session
from flask import Flask, jsonify, redirect, request, session
from werkzeug.exceptions import HTTPException

from engine import Engine, MAX_OUTPUT_TOKENS

ISSUER = "https://huggingface.co"
SESSION_SECONDS = 8 * 60 * 60
MAX_BODY_BYTES = 16384
MAX_STORE_RESPONSE_BYTES = 2 * 1024 * 1024
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
LIMITS = {"prompt_characters": 2000, "output_tokens": MAX_OUTPUT_TOKENS,
          "daily_user_runs": 20, "daily_global_runs": 200, "conversations": 100}


class ApiError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


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


class Store:
    _dns_lock = threading.Lock()

    def __init__(self, url, secret):
        self.url = url
        self.secret = secret
        self.configured = (valid_store_url(url) and isinstance(secret, str)
                           and bool(re.fullmatch(r"[\x21-\x7e]{32,256}", secret)))

    def call(self, op, **data):
        if not self.configured:
            raise ApiError("store_unavailable", 503)
        # On this Space, one address family fails immediately and the other
        # consumes a 3.05s connect budget. Try IPv6 first, then IPv4 longer.
        # The lock keeps one call from restoring dual-stack DNS under another.
        with self._dns_lock:
            for family, timeout in ((socket.AF_INET6, (1, 15)), (socket.AF_INET, (8, 15))):
                try:
                    return self._post(op, data, family, timeout)
                except requests.RequestException as exc:
                    print("store_transport_failed "
                          f"family={family} error={type(exc).__name__} "
                          f"{self._failure_diagnostic(exc)}",
                          file=sys.stderr, flush=True)
                    # A mutation may already have committed when its response is lost.
                    # Only replay reads, or failures known to precede request dispatch.
                    if not self._retry_safe(op, exc):
                        raise ApiError("store_unavailable", 503) from None
            raise ApiError("store_unavailable", 503)

    @staticmethod
    def _failure_diagnostic(exc):
        """Versions and exception-chain function names. No message or headers."""
        import urllib3
        labels = []
        current = exc
        seen = set()
        while current is not None and len(seen) < 8 and id(current) not in seen:
            seen.add(id(current))
            labels.append(type(current).__name__)
            tb = current.__traceback__
            if tb is not None:
                for frame in traceback.extract_tb(tb)[-12:]:
                    name = frame.name or ""
                    if name in {"<lambda>", "<module>"} or re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_]{0,80}", name):
                        labels.append(name)
            if current.__cause__ is not None:
                current = current.__cause__
            elif current.__suppress_context__:
                current = None
            else:
                current = current.__context__
        return (f"requests={requests.__version__} urllib3={urllib3.__version__} "
                f"chain={','.join(labels)}")

    @staticmethod
    def _retry_safe(op, exc):
        if op in {"check", "list", "get"} or isinstance(exc, requests.ConnectTimeout):
            return True
        # Requests wraps urllib3 connection-establishment failures this way.
        # A bare ConnectionError or a ProtocolError can occur after a commit.
        wrapped = exc.args[0] if len(exc.args) == 1 else None
        return (isinstance(exc, requests.ConnectionError)
                and isinstance(wrapped, MaxRetryError)
                and isinstance(wrapped.reason, NewConnectionError))

    def _post(self, op, data, family, timeout):
        previous_family = urllib3_connection.allowed_gai_family
        urllib3_connection.allowed_gai_family = lambda: family
        try:
            # A new client avoids sharing mutable cookies or adapters across threads.
            with requests.Session() as client:
                client.trust_env = False
                with client.post(self.url + "/v1/store", json={"op": op, **data},
                                 headers={"Authorization": "Bearer " + self.secret,
                                          "Accept": "application/json"},
                                 timeout=timeout, allow_redirects=False, stream=True) as response:
                    if response.status_code not in {200, 400, 404, 409, 413, 429}:
                        raise ApiError("store_unavailable", 503)
                    if response.headers.get("content-type", "").split(";")[0] != "application/json":
                        raise ApiError("store_unavailable", 503)
                    content = bytearray()
                    for chunk in response.iter_content(chunk_size=16384):
                        content.extend(chunk)
                        if len(content) > MAX_STORE_RESPONSE_BYTES:
                            raise ApiError("store_unavailable", 503)
                    body = json.loads(content)
                    if not isinstance(body, dict):
                        raise ApiError("store_unavailable", 503)
                    if response.status_code != 200:
                        aliases = {"not_found": "not_found",
                                   "idempotency_conflict": "idempotency_conflict",
                                   "conversation_unresolved": "conversation_pending",
                                   "conversation_limit": "quota_exceeded",
                                   "conversation_run_limit": "quota_exceeded",
                                   "user_daily_limit": "quota_exceeded",
                                   "global_daily_limit": "quota_exceeded",
                                   "context_changed": "context_changed"}
                        code = aliases.get(body.get("error"))
                        if code is None:
                            raise ApiError("store_unavailable", 503)
                        raise ApiError(code, response.status_code)
                    return body
        except ApiError:
            raise
        except requests.RequestException:
            raise
        except (ValueError, TypeError):
            raise ApiError("store_unavailable", 503) from None
        finally:
            urllib3_connection.allowed_gai_family = previous_family


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

    def store_ready():
        try:
            result = store.call("check")
            return (result.get("status") == "available" and result.get("schema_version") == 1
                    and result.get("receipt_minted") is False)
        except ApiError:
            return False

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
        result = {"auth": auth_ready, "store": store_ready(), "model": bool(engine.ready)}
        return jsonify(ready=result, receipt_minted=False), 200 if all(result.values()) else 503

    @app.get("/api/session")
    def session_info():
        # Only browser session state is initialized; no store writes or receipts.
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        user = current_user()
        return jsonify(user={"name": user["name"]} if user else None,
                       csrf=session["csrf"], limits=LIMITS,
                       ready={"auth": auth_ready, "store": store_ready(), "model": bool(engine.ready)})

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
        result = store.call("purge", owner=user["owner"])
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
