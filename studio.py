"""Loopback-only browser UI for the existing curl-backed New agent runner."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit
import urllib.request
import uuid
import webbrowser

import app
import new_agent
import session_recovery
from cloud_connection import CloudConnection
from session_lock import SessionBusy, exclusive_lock
from session_control import ROOT_TURN_OPEN_EVENTS


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / ".studio"
MAX_MESSAGE = 32_000
MAX_BODY = 140_000
MAX_JOBS = 30
MAX_EVENTS = 2000
MAX_OUTPUT = 1_000_000
MAX_SUBMISSIONS = 10_000
JOB_ID = re.compile(r"[0-9a-f]{32}")
SUBMISSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/studio.js": ("studio.js", "text/javascript; charset=utf-8"),
    "/studio.css": ("studio.css", "text/css; charset=utf-8"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/vendor/lucide.min.js": ("vendor/lucide.min.js", "text/javascript; charset=utf-8"),
    "/vendor/marked.umd.js": ("vendor/marked.umd.js", "text/javascript; charset=utf-8"),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def parse_status(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    documents = []
    remaining = text.strip()
    while remaining:
        value, end = decoder.raw_decode(remaining)
        documents.append(value)
        remaining = remaining[end:].lstrip()
    if (len(documents) != 3 or not all(isinstance(value, dict) for value in documents)
            or "latest_turn" not in documents[1] or not isinstance(documents[2].get("data"), list)):
        raise ValueError("The status response did not contain session, turn, and item records.")
    return {"session": documents[0], "latest_turn": documents[1]["latest_turn"], "items": documents[2]}


def partial_text(events: list[dict[str, Any]]) -> str:
    parts: dict[tuple[Any, Any, Any], str] = {}
    for event in events:
        data = event.get("data")
        if event.get("type") != "event" or not isinstance(data, dict):
            continue
        key = (data.get("item_id"), data.get("output_index"), data.get("content_index"))
        if data.get("type") == "agent.session.turn.output_text.delta" and isinstance(data.get("delta"), str):
            parts[key] = parts.get(key, "") + data["delta"]
        elif data.get("type") == "agent.session.turn.output_text.done" and isinstance(data.get("text"), str):
            parts[key] = data["text"]
    return "\n".join(parts.values())[:MAX_OUTPUT]


class StudioError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Studio:
    def __init__(self, data_dir: Path = DATA_DIR, state_path: Path = new_agent.STATE_PATH,
                 runner: Callable[[dict[str, Any], Callable[[str, Any], None]], tuple[int, str]] | None = None,
                 cancel_runner: Callable[[dict[str, Any], dict[str, Any]], None] | None = None):
        self.data_dir = data_dir
        self.state_path = state_path
        self.token = secrets.token_urlsafe(32)
        self.instance_id = uuid.uuid4().hex
        self.origin = ""
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.submissions: dict[str, dict[str, Any]] = {}
        self.controls: dict[str, subprocess.Popen[str]] = {}
        self.busy_job_id: str | None = None
        self.runner = runner or self._run_cli
        self.cancel_runner = cancel_runner or self._send_cancel
        self.worker: threading.Thread | None = None
        self.cloud_connection = CloudConnection()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        submission_file = self.data_dir / "submissions.json"
        if submission_file.exists():
            saved = json.loads(submission_file.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or any(not SUBMISSION_ID.fullmatch(key) or not isinstance(value, dict)
                                                  or not JOB_ID.fullmatch(str(value.get("job_id", "")))
                                                  for key, value in saved.items()):
                raise StudioError("Saved submission identities are invalid. Preserve them for inspection.")
            self.submissions = saved
        session_file = self.data_dir / "sessions.json"
        if session_file.exists():
            saved = json.loads(session_file.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise StudioError("Saved Studio session index is invalid. Preserve it for inspection.")
            self.sessions = saved
        for path in sorted(self.data_dir.glob("job-*.json")):
            job = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(job, dict) or not JOB_ID.fullmatch(str(job.get("id", ""))):
                raise StudioError("A saved Studio job is invalid. Preserve it for inspection.")
            if job.get("status") in ("queued", "running"):
                job["status"] = "pending"
                job["can_cancel"] = False
                if (job.get("cancellation") or {}).get("status") == "requested":
                    job["cancellation"]["status"] = "unknown"
                job["error"] = "Studio stopped before the outcome was observed. Refresh the saved session; do not automatically submit again."
                self._persist(job)
            self.jobs[job["id"]] = job
        self._prune()
        state = self._state()
        session_id = state.get("session_id")
        if isinstance(session_id, str) and SESSION_ID.fullmatch(session_id) and session_id not in self.sessions:
            self.sessions[session_id] = {"id": session_id, "title": "Saved session", "updated_at": now()}
            atomic_json(self.data_dir / "sessions.json", self.sessions)

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            key = os.environ.get("OPENAI_API_KEY", "")
            if key:
                value = value.replace(key, "[credential redacted]")
            value = value.replace(self.token, "[local capability redacted]")
            value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [redacted]", value)
            return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[credential redacted]", value)
        if isinstance(value, dict):
            return {str(key): self._sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        return value

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("project_id") != app.PROJECT_ID:
            raise StudioError("The saved agent state is invalid or belongs to another project.")
        return state

    def _persist(self, job: dict[str, Any]) -> None:
        cleaned = self._sanitize(job)
        job.clear()
        job.update(cleaned)
        job["updated_at"] = now()
        previous = job.get("receipt", {}).get("digest")
        payload = {key: value for key, value in job.items() if key != "receipt"}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        record = {"schema": "szl.local-agent-operation/v1", "evidence_class": "MEASURED",
                  "scope": "local operation record; not a provider signature or independent witness",
                  "signed": False, "job_id": job["id"], "action": job["action"],
                  "status": job["status"], "observed_at": job["updated_at"],
                  "payload_sha256": digest, "previous_digest": previous}
        record["digest"] = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        job["receipt"] = record
        atomic_json(self.data_dir / ("job-" + job["id"] + ".json"), job)

    def _prune(self) -> None:
        ordered = sorted(self.jobs.values(), key=lambda job: job.get("created_at", ""))
        for job in ordered:
            if len(self.jobs) <= MAX_JOBS:
                break
            if job.get("status") in ("queued", "running", "pending"):
                continue
            (self.data_dir / ("job-" + job["id"] + ".json")).unlink(missing_ok=True)
            self.jobs.pop(job["id"], None)

    def submission(self, submission_id: str) -> dict[str, Any]:
        if not SUBMISSION_ID.fullmatch(submission_id):
            raise StudioError("Invalid client submission ID.")
        with self.lock:
            record = self.submissions.get(submission_id)
            if record is None:
                raise StudioError("The local submission was not found.", 404)
            job = self.jobs.get(record["job_id"])
            return {"client_submission_id": submission_id, "job_id": record["job_id"],
                    "status": job["status"] if job else record["status"], "retained": job is not None,
                    "observed_session_id": record.get("observed_session_id"),
                    "provider_terminal_status": record.get("provider_terminal_status")}

    def _submission_status(self, job: dict[str, Any]) -> None:
        submission_id = job.get("client_submission_id")
        if submission_id in self.submissions:
            records = dict(self.submissions)
            records[submission_id] = dict(records[submission_id], status=job["status"])
            if isinstance(job.get("session_id"), str) and SESSION_ID.fullmatch(job["session_id"]):
                records[submission_id]["observed_session_id"] = job["session_id"]
            if job.get("provider_terminal_status") in ("completed", "cancelled", "failed"):
                records[submission_id]["provider_terminal_status"] = job["provider_terminal_status"]
            atomic_json(self.data_dir / "submissions.json", records)
            self.submissions = records

    def state(self) -> dict[str, Any]:
        with self.lock:
            state = self._state()
            definition, _ = new_agent._definition()
            summaries = [{key: job.get(key) for key in ("id", "action", "target_job_id", "title", "message", "session_id", "root_turn_id", "can_cancel", "cancellation", "client_submission_id", "status", "created_at", "updated_at", "error")}
                         for job in sorted(self.jobs.values(), key=lambda job: job["created_at"], reverse=True)]
            return self._sanitize({
                "instance_id": self.instance_id,
                "profile": {"name": new_agent.AGENT_NAME, "model": definition["model"],
                            "project_id": app.PROJECT_ID, "agent_id": state.get("agent_id"),
                            "credential_available": bool(os.environ.get("OPENAI_API_KEY", "").strip())},
                "current_session_id": state.get("session_id"),
                "sessions": sorted(self.sessions.values(), key=lambda session: session.get("updated_at", ""), reverse=True),
                "jobs": summaries, "busy": self.busy_job_id is not None, "busy_job_id": self.busy_job_id,
                "pending_start": state.get("pending_start"),
                "pending_submissions": list(state.get("pending_submissions", {})),
                "limits": {"message_characters": MAX_MESSAGE, "retained_jobs": MAX_JOBS},
            })

    def submit(self, body: Any) -> str:
        if not isinstance(body, dict) or set(body) - {"action", "message", "session_id", "client_submission_id", "after", "limit"}:
            raise StudioError("Use action, message, and an optional known session_id only.")
        action = body.get("action")
        if action not in ("start", "send", "status", "history"):
            raise StudioError("Choose start, send, status, or history.")
        after, limit = body.get("after"), body.get("limit", 100)
        if action != "history" and ("after" in body or "limit" in body):
            raise StudioError("Pagination is available only for history.")
        if (type(limit) is not int or not 1 <= limit <= 100 or
                (after is not None and (not isinstance(after, str) or not session_recovery.CURSOR.fullmatch(after)))):
            raise StudioError("Use a valid history cursor and integer limit from 1 to 100.")
        message = body.get("message", "")
        if not isinstance(message, str) or len(message) > MAX_MESSAGE or "\0" in message:
            raise StudioError(f"Message must be text of at most {MAX_MESSAGE} characters without NUL bytes.")
        if action in ("start", "send") and not message.strip():
            raise StudioError("Enter a message before starting a turn.")
        if action in ("status", "history") and message:
            raise StudioError("Read actions do not accept a message.")
        submission_id = body.get("client_submission_id")
        if action in ("start", "send") and (not isinstance(submission_id, str) or not SUBMISSION_ID.fullmatch(submission_id)):
            raise StudioError("A UUID client_submission_id is required for every start or send.")
        if action in ("status", "history") and submission_id is not None:
            raise StudioError("Read actions do not accept a submission identity.")
        with self.lock:
            existing = self.submissions.get(submission_id)
            if existing is not None:
                session_id = body.get("session_id") or (existing["session_id"] if action == "send" else None)
                fingerprint = {"action": action, "session_id": session_id,
                               "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest()}
                if any(existing.get(key) != value for key, value in fingerprint.items()) or (action == "start" and body.get("session_id") is not None):
                    raise StudioError("This submission identity belongs to different content. No new request was dispatched.", 409)
                return existing["job_id"]
            if self.busy_job_id is not None:
                raise StudioError("A job is already running. Its outcome is available in the activity view.", 409)
            state = self._state()
            if action == "start" and state.get("pending_start"):
                raise StudioError("A previous start has an unknown outcome. Inspect OpenAI Platform and reconcile the saved marker before starting again.", 409)
            session_id = body.get("session_id") or (state.get("session_id") if action != "start" else None)
            if action == "start" and body.get("session_id") is not None:
                raise StudioError("A new session does not accept a previous session ID.")
            if action != "start":
                if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
                    raise StudioError("Select a saved session first.")
                if session_id not in self.sessions and session_id != state.get("session_id"):
                    raise StudioError("This session is not in the local session index.")
            if action in ("start", "send") and any(
                    job.get("status") == "pending" and job.get("action") in ("start", "send")
                    and ((action == "send" and job.get("session_id") == session_id)
                         or (action == "start" and job.get("action") == "start" and not job.get("session_id")))
                    for job in self.jobs.values()):
                raise StudioError("An earlier local job is unresolved. Check its provider outcome before submitting new work.", 409)
            pending = state.get("pending_submissions", {})
            if action == "send" and isinstance(pending, dict) and pending.get(session_id):
                record = pending[session_id]
                if not isinstance(record, dict) or record.get("message") != message:
                    raise StudioError("A previous message is unresolved. Refresh status first; only the identical message can be reconciled without a new submission.", 409)
            if not os.environ.get("OPENAI_API_KEY", "").strip():
                raise StudioError("No server-side credential is available. Restart with the configured PowerShell launcher.", 503)
            if action in ("start", "send") and sum(job.get("status") == "pending" for job in self.jobs.values()) >= MAX_JOBS:
                raise StudioError("Unresolved job history is full. Preserve and reconcile it before starting more work.", 409)
            if action in ("start", "send") and len(self.submissions) >= MAX_SUBMISSIONS:
                raise StudioError("Durable submission identity storage is full. No identities were forgotten and no new request was dispatched.", 409)
            job_id = uuid.uuid4().hex
            created = now()
            job = {"id": job_id, "action": action, "message": self._sanitize(message),
                   "title": " ".join(message.split())[:80] or "Refresh session",
                   "session_id": session_id, "status": "queued", "created_at": created,
                   "updated_at": created, "events": [], "next_cursor": 0, "result": None,
                   "error": None, "truncated": False, "event_characters": 0,
                   "client_submission_id": submission_id, "root_turn_id": None,
                   "can_cancel": False, "cancellation": None}
            if action == "history":
                job.update(after=after, limit=limit, title="Load saved history")
            if action == "start":
                job["studio_start_nonce"] = secrets.token_hex(32)
            self._persist(job)
            if submission_id is not None:
                records = dict(self.submissions)
                records[submission_id] = {"job_id": job_id, "action": action, "session_id": session_id,
                                          "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                                          "status": "queued", "created_at": created}
                atomic_json(self.data_dir / "submissions.json", records)
                self.submissions = records
            self.jobs[job_id] = job
            self.busy_job_id = job_id
            self._prune()
            # The worker receives the original message; only redacted content is persisted.
            invocation = dict(job, message=message, previous_session_id=state.get("session_id"))
            self.worker = threading.Thread(target=self._work, args=(job, invocation), daemon=True)
            self.worker.start()
            return job_id

    def reconcile(self, target_id: str, body: Any) -> str:
        if body != {}:
            raise StudioError("Recovery accepts an empty JSON object only.")
        with self.lock:
            target = self.jobs.get(target_id)
            if target is None:
                raise StudioError("The original local job was not found.", 404)
            if self.busy_job_id is not None:
                raise StudioError("Wait for the active local job before checking an interrupted outcome.", 409)
            if target.get("action") not in ("start", "send") or target.get("status") not in ("pending", "failed"):
                raise StudioError("Only an interrupted start or send can be reconciled.", 409)
            if not os.environ.get("OPENAI_API_KEY", "").strip():
                raise StudioError("No server-side credential is available.", 503)
            record = self.submissions.get(target.get("client_submission_id"))
            if not isinstance(record, dict) or record.get("job_id") != target_id:
                raise StudioError("The original submission binding is unavailable; no provider request was sent.", 409)
            request = {key: target.get(key) for key in ("id", "action", "session_id", "root_turn_id", "created_at", "client_submission_id", "studio_start_nonce")}
            request["message_sha256"] = record["message_sha256"]
            job_id, created = uuid.uuid4().hex, now()
            job = {"id": job_id, "action": "reconcile", "target_job_id": target_id,
                   "title": "Check provider outcome", "message": "", "session_id": target.get("session_id"),
                   "status": "queued", "created_at": created, "updated_at": created,
                   "events": [], "next_cursor": 0, "result": None, "error": None,
                   "truncated": False, "event_characters": 0, "can_cancel": False}
            self._persist(job)
            self.jobs[job_id] = job
            self.busy_job_id = job_id
            self.worker = threading.Thread(target=self._work, args=(job, dict(job, recovery_request=request)), daemon=True)
            self.worker.start()
            return job_id

    def _apply_recovery(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        target = self.jobs.get(job["target_job_id"])
        if target is None or result.get("target_job_id") != target["id"]:
            raise ValueError("The recovery result does not match the requested local job.")
        if result.get("outcome") != "resolved":
            return
        if (result.get("target_status") not in ("completed", "failed", "cancelled")
                or result.get("provider_terminal_status") not in ("completed", "failed", "cancelled")
                or result.get("pending_resolved") is not True
                or not isinstance(result.get("session_id"), str) or not SESSION_ID.fullmatch(result["session_id"])
                or not isinstance(result.get("turn_id"), str) or not SESSION_ID.fullmatch(result["turn_id"])):
            raise ValueError("The recovery result has no exact terminal proof.")
        if target.get("session_id") not in (None, result["session_id"]) or target.get("root_turn_id") not in (None, result["turn_id"]):
            raise ValueError("The recovery result changed an already observed provider identity.")
        updated = dict(target, status=result["target_status"], session_id=result["session_id"],
                       root_turn_id=result["turn_id"], provider_terminal_status=result["provider_terminal_status"],
                       can_cancel=False, error=None, reconciliation={"job_id": job["id"], "observed_at": now(), "outcome": "resolved"},
                       result={"text": result.get("text", ""), "partial": result.get("partial", True), "recovered": True})
        if result["target_status"] == "failed":
            updated["error"] = "The exact provider turn failed or completed with a saved tool issue. Its outcome was reconciled without resubmission."
        if result["provider_terminal_status"] == "cancelled":
            updated["cancellation"] = {"status": "confirmed", "provider_status": "cancelled", "pending_resolved": True,
                                       "detail": "Exact cancelled root turn confirmed by explicit provider readback."}
        self._persist(updated)
        self.jobs[target["id"]] = updated
        self._submission_status(updated)
        sid = result["session_id"]
        self.sessions[sid] = {"id": sid, "title": self.sessions.get(sid, {}).get("title") or target["title"], "updated_at": now()}
        atomic_json(self.data_dir / "sessions.json", self._sanitize(self.sessions))

    def cancel(self, job_id: str, body: Any) -> dict[str, Any]:
        if body != {}:
            raise StudioError("Cancellation accepts an empty JSON object only.")
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise StudioError("The local job was not found.", 404)
            if job.get("cancellation") is not None:
                return {"job_id": job_id, "cancellation": job["cancellation"]}
            if job_id != self.busy_job_id or job["action"] not in ("start", "send") or job["status"] != "running" or not job.get("can_cancel"):
                raise StudioError("This job has no observed active root turn available to cancel.", 409)
            cancellation = {"status": "requested", "requested_at": now(),
                            "session_id": job["session_id"], "turn_id": job["root_turn_id"],
                            "detail": "A cancellation request is being dispatched; provider confirmation is still required.",
                            "external_writer_race": "Provider cancellation has no turn selector; external project writers can race the latest-turn pre-read."}
            previous_can_cancel = job["can_cancel"]
            job["cancellation"] = cancellation
            job["can_cancel"] = False
            try:
                self._persist(job)
            except OSError:
                job["cancellation"] = None
                job["can_cancel"] = previous_can_cancel
                raise
            request = {"action": "cancel", "session_id": job["session_id"], "turn_id": job["root_turn_id"]}
            try:
                self.cancel_runner(job, request)
            except Exception:
                job["cancellation"] = dict(cancellation, status="unknown", detail="The local cancellation channel failed. Provider cancellation is not confirmed.")
                self._persist(job)
            return {"job_id": job_id, "cancellation": job["cancellation"]}

    def _send_cancel(self, job: dict[str, Any], request: dict[str, Any]) -> None:
        process = self.controls.get(job["id"])
        if process is None or process.poll() is not None or process.stdin is None:
            raise StudioError("The observation process is no longer available.", 409)
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()

    def _event(self, job: dict[str, Any], kind: str, data: Any) -> None:
        with self.lock:
            important = False
            if kind == "event" and isinstance(data, dict):
                session_id = app._event_session_id(data)
                turn = data.get("turn")
                root = (not isinstance(turn, dict) or turn.get("subagent_id") is None) and (session_id is None or job.get("session_id") is None or session_id == job["session_id"])
                if isinstance(turn, dict) and turn.get("session_id") is not None and turn["session_id"] != (session_id or job.get("session_id")):
                    root = False
                if root and session_id and SESSION_ID.fullmatch(session_id) and job.get("session_id") is None:
                    job["session_id"] = session_id
                    important = True
                turn_id = app._event_turn_id(data)
                if data.get("type") in ROOT_TURN_OPEN_EVENTS and root and isinstance(turn, dict) and isinstance(turn.get("id"), str) and SESSION_ID.fullmatch(turn["id"]) and job.get("root_turn_id") is None:
                    job["root_turn_id"] = turn["id"]
                    job["can_cancel"] = job.get("session_id") is not None and (job["id"] in self.controls or self.cancel_runner != self._send_cancel)
                    important = True
                if data.get("type") == "studio.cancel.status" and job.get("cancellation") is not None and data.get("session_id") == job.get("session_id") and data.get("turn_id") == job.get("root_turn_id"):
                    job["cancellation"] = {key: value for key, value in self._sanitize(data).items() if key != "type"}
                    job["can_cancel"] = False
                    important = True
                if root and job.get("root_turn_id") is not None and turn_id == job["root_turn_id"] and data.get("type") in ("agent.session.turn.completed", "agent.session.turn.failed", "agent.session.turn.cancelled"):
                    job["can_cancel"] = False
                    job["provider_terminal_status"] = str(data["type"]).rsplit(".", 1)[-1]
                    if job.get("cancellation") is not None:
                        job["cancellation"]["status"] = "confirmed" if job["provider_terminal_status"] == "cancelled" else "not_active"
                        job["cancellation"]["provider_status"] = job["provider_terminal_status"]
                    elif job["provider_terminal_status"] == "cancelled":
                        job["cancellation"] = {"status": "confirmed", "provider_status": "cancelled",
                                               "session_id": job.get("session_id"), "turn_id": turn_id,
                                               "detail": "Exact provider terminal cancellation observed; no local cancellation was requested."}
                    important = True
            cleaned = self._sanitize(data)
            serialized = json.dumps(cleaned, ensure_ascii=False)
            if len(serialized) > 32_000:
                cleaned = serialized[:32_000] + " [event truncated]"
                job["truncated"] = True
            event = {"seq": job["next_cursor"], "type": kind, "data": cleaned}
            job["next_cursor"] += 1
            job["events"].append(event)
            job["event_characters"] = job.get("event_characters", 0) + len(json.dumps(event, ensure_ascii=False))
            while len(job["events"]) > MAX_EVENTS or job["event_characters"] > MAX_OUTPUT:
                removed = job["events"].pop(0)
                job["event_characters"] -= len(json.dumps(removed, ensure_ascii=False))
                job["truncated"] = True
            if important:
                self._persist(job)

    def _work(self, job: dict[str, Any], invocation: dict[str, Any]) -> None:
        try:
            with self.lock:
                job["status"] = "running"
                self._persist(job)
            code, stdout = self.runner(invocation, lambda kind, data: self._event(job, kind, data))
            with self.lock:
                state = self._state()
                observed = job.get("session_id")
                if isinstance(observed, str) and SESSION_ID.fullmatch(observed):
                    job["session_id"] = observed
                    old = self.sessions.get(observed, {})
                    self.sessions[observed] = {"id": observed, "title": old.get("title") or job["title"], "updated_at": now()}
                    atomic_json(self.data_dir / "sessions.json", self._sanitize(self.sessions))
                if code == 0 and job["action"] in ("history", "reconcile"):
                    result = json.loads(stdout)
                    if not isinstance(result, dict):
                        raise ValueError("The readback result is invalid.")
                    if job["action"] == "reconcile":
                        self._apply_recovery(job, result)
                    job["result"] = self._sanitize(result)
                else:
                    job["result"] = self._sanitize(parse_status(stdout) if code == 0 and job["action"] == "status" else {"text": stdout})
                cancelled = job.get("provider_terminal_status") == "cancelled" or (job.get("cancellation") or {}).get("status") == "confirmed"
                if cancelled:
                    job["status"] = "cancelled"
                    job["result"] = {"text": self._sanitize(stdout or partial_text(job["events"])), "partial": True,
                                     "truncated": job.get("truncated", False)}
                elif code == 2 or (job["action"] == "start" and (state.get("pending_start") or observed is None)):
                    job["status"] = "pending"
                    job["error"] = "The provider outcome is not fully observed. Refresh the saved session before any deliberate retry."
                elif code == 0:
                    job["status"] = "completed"
                else:
                    job["status"] = "failed"
                    logs = [event["data"] for event in job["events"] if event["type"] == "log" and isinstance(event["data"], str)]
                    job["error"] = logs[-1][-2000:] if logs else "The agent command failed. Inspect the activity and refresh saved state."
        except Exception as exc:
            with self.lock:
                job["status"] = "pending"
                job["error"] = "The local runner could not determine the outcome. Inspect saved state before submitting again."
                self._event(job, "log", f"Local runner error: {type(exc).__name__}")
        finally:
            with self.lock:
                try:
                    job["can_cancel"] = False
                    if (job.get("cancellation") or {}).get("status") == "requested":
                        job["cancellation"]["status"] = "unknown"
                    self._persist(job)
                    self._submission_status(job)
                    self._prune()
                finally:
                    self.busy_job_id = None

    def _run_cli(self, job: dict[str, Any], emit: Callable[[str, Any], None]) -> tuple[int, str]:
        arguments = [sys.executable, str(ROOT / "new_agent.py"), job["action"]]
        message_path = self.data_dir / ("input-" + job["id"] + ".txt")
        recovery_path = self.data_dir / ("recovery-" + job["id"] + ".json")
        process = None
        if job["action"] in ("start", "send"):
            message_path.write_text(job["message"], encoding="utf-8", newline="")
            arguments.extend(["--message-file", str(message_path), "--raw-events", "--studio-control"])
            if job["action"] == "start":
                arguments.extend(["--studio-job-id", job["id"], "--studio-start-nonce", job["studio_start_nonce"]])
            elif job.get("client_submission_id"):
                arguments.extend(["--idempotency-key", job["client_submission_id"]])
        elif job["action"] == "status":
            arguments.append("--items")
        elif job["action"] == "history":
            arguments.extend(["--limit", str(job["limit"])])
            if job.get("after") is not None:
                arguments.extend(["--after", job["after"]])
        elif job["action"] == "reconcile":
            atomic_json(recovery_path, job["recovery_request"])
            arguments.extend(["--request-file", str(recovery_path)])
        if job.get("session_id") and job["action"] != "reconcile":
            arguments.extend(["--session-id", job["session_id"]])
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        output: list[str] = []
        total = [0]
        overflow = [False]
        readers: list[threading.Thread] = []
        try:
            process = subprocess.Popen(arguments, cwd=ROOT, env=environment, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       encoding="utf-8", errors="replace", bufsize=1,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self.lock:
                self.controls[job["id"]] = process
            def read(stream: Any, channel: str) -> None:
                for line in iter(lambda: stream.readline(32_001), ""):
                    if channel == "output":
                        if total[0] + len(line) <= MAX_OUTPUT:
                            output.append(line)
                            emit(channel, line)
                        else:
                            overflow[0] = True
                        total[0] += len(line)
                    else:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            event = None
                        emit("event", event) if isinstance(event, dict) and "type" in event else emit("log", line.rstrip())
            for stream, channel in ((process.stdout, "output"), (process.stderr, "log")):
                thread = threading.Thread(target=read, args=(stream, channel), daemon=True)
                readers.append(thread)
                thread.start()
            try:
                code = process.wait(timeout=1950)
            except subprocess.TimeoutExpired:
                emit("log", "Local observation timed out. This does not cancel provider work; refresh saved state.")
                process.kill()
                code = 2
            for reader in readers:
                reader.join(timeout=10)
            if any(reader.is_alive() for reader in readers):
                code = 2
                emit("log", "Output observation did not finish; outcome remains unknown.")
            if overflow[0]:
                emit("log", "Output exceeded the local display bound. Read the saved provider session for the complete answer.")
                code = 2
            return code, "".join(output)
        finally:
            with self.lock:
                self.controls.pop(job["id"], None)
            if process is not None:
                if process.stdin:
                    process.stdin.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if process.stdout and not any(reader.is_alive() for reader in readers):
                    process.stdout.close()
                if process.stderr and not any(reader.is_alive() for reader in readers):
                    process.stderr.close()
            message_path.unlink(missing_ok=True)
            recovery_path.unlink(missing_ok=True)

    def job(self, job_id: str, after: int) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise StudioError("The local job was not found.", 404)
            result = {key: value for key, value in job.items() if key != "events"}
            result["events"] = [event for event in job["events"] if event["seq"] >= after]
            return self._sanitize(result)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def get_request(self) -> Any:
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def make_server(studio: Studio, port: int, static_dir: Path = ROOT / "studio") -> LocalServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _response(self, status: int, value: Any, content_type: str = "application/json; charset=utf-8") -> None:
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _guard(self, api: bool, write: bool = False) -> None:
            if self.headers.get_all("Host", []) != [urlsplit(studio.origin).netloc]:
                raise StudioError("Unexpected local host.", 403)
            origins = self.headers.get_all("Origin", [])
            if origins and origins != [studio.origin]:
                raise StudioError("Cross-origin requests are not allowed.", 403)
            if write and origins != [studio.origin]:
                raise StudioError("A same-origin browser request is required.", 403)
            if self.headers.get("Sec-Fetch-Site") in ("cross-site",):
                raise StudioError("Cross-site requests are not allowed.", 403)
            if api:
                tokens = self.headers.get_all("X-Studio-Token", [])
                if len(tokens) != 1 or not secrets.compare_digest(tokens[0], studio.token):
                    raise StudioError("Open Studio from the local launcher to authorize this browser.", 401)

        def do_GET(self) -> None:
            try:
                parts = urlsplit(self.path)
                if parts.scheme or parts.netloc or not self.path.startswith("/"):
                    raise StudioError("Unsupported request target.")
                path = parts.path
                api = path.startswith("/api/")
                self._guard(api)
                if path == "/api/state":
                    self._response(200, studio.state())
                elif path == "/api/cloud-status":
                    if parts.query or parts.fragment or "?" in self.path or "#" in self.path:
                        raise StudioError("Cloud status does not accept query parameters or fragments.")
                    self._response(200, studio.cloud_connection.check())
                elif path.startswith("/api/submissions/"):
                    self._response(200, studio.submission(path.removeprefix("/api/submissions/")))
                elif path.startswith("/api/jobs/") and JOB_ID.fullmatch(path.removeprefix("/api/jobs/")):
                    query = parse_qs(parts.query, strict_parsing=True)
                    if set(query) - {"after"} or len(query.get("after", ["0"])) != 1:
                        raise StudioError("Use only one numeric after cursor.")
                    raw_cursor = query.get("after", ["0"])[0]
                    if not raw_cursor.isdigit() or len(raw_cursor) > 10:
                        raise StudioError("Invalid event cursor.")
                    self._response(200, studio.job(path.removeprefix("/api/jobs/"), int(raw_cursor)))
                elif path in STATIC_FILES:
                    filename, content_type = STATIC_FILES[path]
                    self._response(200, (static_dir / filename).read_bytes(), content_type)
                else:
                    self._response(404, {"error": "Not found."})
            except StudioError as exc:
                self._response(exc.status, {"error": str(exc)})
            except (ValueError, OSError, app.AppError):
                self._response(503, {"error": "The local resource is unavailable or invalid. Inspect the launcher for setup issues."})

        def do_POST(self) -> None:
            try:
                self._guard(True, True)
                cancel_match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/cancel", self.path)
                reconcile_match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/reconcile", self.path)
                if self.path != "/api/jobs" and cancel_match is None and reconcile_match is None:
                    raise StudioError("Not found.", 404)
                if self.headers.get_all("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    raise StudioError("A single bounded Content-Length is required.")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise StudioError("Use application/json.", 415)
                size_text = self.headers["Content-Length"]
                if not size_text.isdigit() or len(size_text) > 8:
                    raise StudioError("Invalid Content-Length.")
                size = int(size_text)
                if size > MAX_BODY:
                    raise StudioError("Request is too large.", 413)
                self.connection.settimeout(10)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise StudioError("Incomplete request body.")
                body = json.loads(raw.decode("utf-8"))
                if cancel_match is not None:
                    self._response(202, studio.cancel(cancel_match[1], body))
                elif reconcile_match is not None:
                    self._response(202, {"job_id": studio.reconcile(reconcile_match[1], body)})
                else:
                    submission_id = body.get("client_submission_id") if isinstance(body, dict) else None
                    replayed = isinstance(submission_id, str) and submission_id in studio.submissions
                    job_id = studio.submit(body)
                    self._response(202, {"job_id": job_id, "replayed": replayed, "retained": job_id in studio.jobs})
            except StudioError as exc:
                self._response(exc.status, {"error": str(exc)})
            except (ValueError, UnicodeError, TimeoutError):
                self._response(400, {"error": "Invalid or incomplete JSON request."})
            except OSError:
                self._response(503, {"error": "Local persistence failed. Inspect saved state before retrying."})

        def do_OPTIONS(self) -> None:
            self._response(405, {"error": "Cross-origin access is not supported."})

    server = LocalServer(("127.0.0.1", port), Handler)
    studio.origin = f"http://127.0.0.1:{server.server_port}"
    return server


def existing_instance(data_dir: Path) -> str | None:
    try:
        metadata = json.loads((data_dir / "server.json").read_text(encoding="utf-8"))
        origin = metadata["origin"]
        parsed = urlsplit(origin)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path or parsed.query
                or parsed.fragment or parsed.username or parsed.password or not parsed.port):
            return None
        token = metadata["token"]
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
            return None
        request = urllib.request.Request(origin + "/api/state", headers={"X-Studio-Token": token})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request: Any, fp: Any, code: int, message: str, headers: Any, url: str) -> None:
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=3) as response:
            if response.url != origin + "/api/state":
                return None
            state = json.load(response)
        if state.get("instance_id") == metadata["instance_id"]:
            return origin + "/#token=" + quote(token, safe="")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local New agent browser interface.")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        with exclusive_lock(DATA_DIR / "server.lock"):
            studio = Studio()
            server = None
            for port in [args.port, *range(args.port + 1, min(args.port + 21, 65536))]:
                try:
                    server = make_server(studio, port)
                    break
                except OSError as exc:
                    if exc.errno not in (48, 98, 10048) and getattr(exc, "winerror", None) != 10048:
                        raise
            if server is None:
                raise StudioError("The requested local ports are busy. Choose another -Port.")
            metadata = {"origin": studio.origin, "token": studio.token, "instance_id": studio.instance_id, "pid": os.getpid()}
            atomic_json(DATA_DIR / "server.json", metadata)
            launch_url = studio.origin + "/#token=" + quote(studio.token, safe="")
            print(f"SZL Agent Studio is listening at {studio.origin}. Local access only.", flush=True)
            print("Use Start-Agents.cmd again to reopen the authorized browser. Closing a tab does not cancel provider work.", flush=True)
            if args.open_browser:
                webbrowser.open(launch_url)
            try:
                server.serve_forever(poll_interval=0.3)
            except KeyboardInterrupt:
                print("Studio stopped. Unobserved jobs remain pending for later inspection.")
            finally:
                server.server_close()
                (DATA_DIR / "server.json").unlink(missing_ok=True)
            return 0
    except SessionBusy:
        url = existing_instance(DATA_DIR)
        if url:
            if args.open_browser:
                webbrowser.open(url)
            print("The existing local Studio instance is ready.")
            return 0
        print("Studio is already starting or running, but could not be verified. No second instance was opened.", file=sys.stderr)
        return 1
    except (OSError, ValueError, StudioError):
        print("Studio could not start. Check local files and the launcher configuration; no provider request was made by startup.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
