"""Private cancellation channel inside the CLI process that owns the session lock."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from typing import Any, Callable

import app


IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,200}")
ROOT_TURN_OPEN_EVENTS = frozenset({"agent.session.turn.created", "agent.session.turn.in_progress",
                                  "agent.session.turn.started"})


class SessionControl:
    def __init__(self, state: dict[str, Any], message: str,
                 emit: Callable[[dict[str, Any]], None] | None = None,
                 session_id: str | None = None):
        self.state = state
        self.message = message
        self.emit = emit or (lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
        self.lock = threading.RLock()
        self.session_id: str | None = session_id
        self.turn_id: str | None = None
        self.terminal: str | None = None
        self.requested = False
        self.finished = False
        self.thread: threading.Thread | None = None

    def _report(self, status: str, detail: str, **extra: Any) -> None:
        self.emit({"type": "studio.cancel.status", "status": status, "detail": detail,
                   "session_id": self.session_id, "turn_id": self.turn_id,
                   "external_writer_race": "Provider cancel has no turn selector; external project writers can race the latest-turn pre-read.",
                   **extra})

    def observe(self, event: dict[str, Any]) -> None:
        with self.lock:
            turn = event.get("turn")
            if isinstance(turn, dict) and turn.get("subagent_id") is not None:
                return
            session_id = app._event_session_id(event)
            nested_session = turn.get("session_id") if isinstance(turn, dict) else None
            if nested_session is not None and nested_session != (session_id or self.session_id):
                return
            if session_id and self.session_id is not None and session_id != self.session_id:
                return
            if session_id and IDENTIFIER.fullmatch(session_id):
                self.session_id = session_id
            kind = event.get("type")
            turn_id = app._event_turn_id(event)
            if kind in ROOT_TURN_OPEN_EVENTS and isinstance(turn, dict) and isinstance(turn.get("id"), str) and IDENTIFIER.fullmatch(turn["id"]):
                if self.turn_id is None:
                    self.turn_id = turn["id"]
            if self.turn_id is not None and kind in ("agent.session.turn.completed", "agent.session.turn.failed", "agent.session.turn.cancelled") and turn_id == self.turn_id:
                self.terminal = str(kind).rsplit(".", 1)[-1]
                if self.requested:
                    self._report("confirmed" if self.terminal == "cancelled" else "not_active",
                                 "The exact observed root turn reached a terminal provider event.", provider_status=self.terminal)

    def request_cancel(self, request: Any) -> None:
        with self.lock:
            if (not isinstance(request, dict) or set(request) != {"action", "session_id", "turn_id"}
                    or request.get("action") != "cancel"
                    or request.get("session_id") != self.session_id
                    or request.get("turn_id") != self.turn_id
                    or not self.session_id or not self.turn_id):
                self._report("not_active", "Cancellation identity does not match the observed root turn.")
                return
            if self.finished or self.terminal is not None:
                self._report("not_active", "The observed root turn is no longer active.", provider_status=self.terminal)
                return
            if self.requested:
                return
            self.requested = True
            try:
                latest = app._latest_turn(self.session_id)
                if not isinstance(latest, dict) or latest.get("id") != self.turn_id or latest.get("subagent_id") is not None:
                    self._report("not_active", "The latest provider root turn no longer matches; no cancellation was sent.")
                    return
                if latest.get("status") not in ("queued", "in_progress", "running"):
                    self.terminal = latest.get("status") if latest.get("status") in ("completed", "failed", "cancelled") else None
                    self._report("confirmed" if self.terminal == "cancelled" else "not_active",
                                 "The provider turn is not cancellable; no cancellation was sent.", provider_status=latest.get("status"))
                    return
                app._api_json("POST", f"/agents/sessions/{self.session_id}/events",
                              {"events": [{"type": "agent.session.input.cancel"}]})
                self._report("requested", "Cancellation was accepted for processing; terminal provider evidence is still required.")
                latest = app._latest_turn(self.session_id)
                if isinstance(latest, dict) and latest.get("id") == self.turn_id and latest.get("status") in ("cancelled", "completed", "failed"):
                    self.terminal = latest["status"]
                    self._report("confirmed" if self.terminal == "cancelled" else "not_active",
                                 "The exact provider turn was read back in a terminal state.", provider_status=self.terminal)
            except Exception:
                self._report("unknown", "Cancellation could not be confirmed. The run may still be active; no automatic retry was made.")

    def start(self) -> None:
        def listen() -> None:
            buffer = b""
            while not self.finished:
                # Raw descriptor reads avoid holding TextIO's lock at interpreter shutdown.
                chunk = os.read(sys.stdin.fileno(), 2048)
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > 4096:
                    return
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        request = json.loads(line)
                    except ValueError:
                        continue
                    self.request_cancel(request)
        self.thread = threading.Thread(target=listen, daemon=True)
        self.thread.start()

    def finish(self) -> None:
        with self.lock:
            self.finished = True
            if self.terminal != "cancelled" or not self.session_id or not self.turn_id:
                return
            try:
                current_state = app._load_state()
                pending = current_state.get("pending_submissions", {})
                record = pending.get(self.session_id) if isinstance(pending, dict) else None
                if not isinstance(record, dict) or record.get("message") != self.message:
                    return
                items = app._turn_items(self.session_id, self.turn_id)
                if not any(self.message in app._item_text(item, "input_text") for item in items
                           if item.get("type") == "message" and item.get("role") == "user"):
                    self._report("confirmed", "Cancellation is confirmed, but its pending message could not be reconciled.", pending_resolved=False)
                    return
                history = current_state.setdefault("resolved_submissions", {})
                key = record.get("idempotency_key")
                if not isinstance(history, dict) or not isinstance(key, str):
                    return
                history[key] = {"session_id": self.session_id, "turn_id": self.turn_id,
                                "message_sha256": hashlib.sha256(self.message.encode("utf-8")).hexdigest(),
                                "status": "cancelled"}
                pending.pop(self.session_id, None)
                app._save_state(current_state)
                self._report("confirmed", "The exact cancelled turn and matching pending submission were reconciled.", pending_resolved=True)
            except Exception:
                self._report("confirmed", "Cancellation is confirmed; pending-message reconciliation remains unavailable.", pending_resolved=False)
