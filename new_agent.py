"""Create and run the user's reusable New agent through the curl-backed API client."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid
from typing import Any, Iterator
from urllib.parse import quote

import app
from session_lock import SessionBusy, exclusive_lock
from session_control import SessionControl
import session_recovery


ROOT = Path(__file__).resolve().parent
DEFINITION_PATH = ROOT / "new_agent.json"
STATE_PATH = ROOT / ".new-agent-state.json"
AGENT_NAME = "New agent"
DEFAULT_MESSAGE = "What can you help me research today?"


def _definition() -> tuple[dict[str, Any], str]:
    try:
        raw = DEFINITION_PATH.read_bytes()
        definition = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise app.AppError(f"Cannot read agent definition: {DEFINITION_PATH}") from exc
    if not isinstance(definition, dict) or set(definition) != {
        "tools", "multi_agent", "name", "model", "instructions", "reasoning", "text"
    }:
        raise app.AppError("new_agent.json must contain the seven agent definition fields.")
    if (definition.get("name") != AGENT_NAME or
            definition.get("model") != "gpt-6-astra" or
            not isinstance(definition.get("instructions"), str)):
        raise app.AppError("new_agent.json must define New agent with gpt-6-astra and string instructions.")
    tools = definition.get("tools")
    if (not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict) or
            tools[0].get("type") != "web_search" or tools[0].get("mode") != "live"):
        raise app.AppError("New agent must use only live web_search; no local tool handler is configured.")
    if definition.get("multi_agent") != {"enabled": False}:
        raise app.AppError("New agent must have multi_agent disabled.")
    return definition, hashlib.sha256(raw).hexdigest()


def _load_profile_state(definition_sha256: str) -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"project_id": app.PROJECT_ID}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise app.AppError(f"Cannot read New agent state: {STATE_PATH}") from exc
    if not isinstance(state, dict) or state.get("project_id") != app.PROJECT_ID:
        raise app.AppError("New agent state belongs to another project or is invalid.")
    agent_id = state.get("agent_id")
    if agent_id is not None:
        if not isinstance(agent_id, str) or not re.fullmatch(r"agent_[A-Za-z0-9_-]+", agent_id):
            raise app.AppError("New agent state contains an invalid agent ID.")
        if state.get("definition_sha256") != definition_sha256:
            raise app.AppError("new_agent.json changed after agent creation. Restore its saved definition or use a new state file after reviewing the change.")
    return state


@contextmanager
def _bound_app(state: dict[str, Any]) -> Iterator[None]:
    """Reuse the proven session transport with an isolated profile state path."""
    previous = (app.STATE_PATH, app.AGENT_ID, app.AGENT_NAME)
    app.STATE_PATH = STATE_PATH
    app.AGENT_ID = state.get("agent_id") or ""
    app.AGENT_NAME = AGENT_NAME
    try:
        yield
    finally:
        app.STATE_PATH, app.AGENT_ID, app.AGENT_NAME = previous


def _matches_definition(saved: Any, definition: dict[str, Any]) -> bool:
    if not isinstance(saved, dict):
        return False
    if (saved.get("name") != definition["name"] or
            saved.get("model") != definition["model"] or
            (saved.get("instructions") or "") != definition["instructions"]):
        return False
    tools = saved.get("tools")
    if (not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict) or
            tools[0].get("type") != "web_search" or tools[0].get("mode") != "live" or
            tools[0].get("context_size") != "medium" or
            tools[0].get("allowed_domains") is not None or tools[0].get("location") is not None):
        return False
    multi_agent = saved.get("multi_agent")
    if not isinstance(multi_agent, dict) or multi_agent.get("enabled") is not False:
        return False
    reasoning = saved.get("reasoning")
    text = saved.get("text")
    return (isinstance(reasoning, dict) and
            reasoning.get("effort") == definition["reasoning"]["effort"] and
            reasoning.get("summary") == definition["reasoning"]["summary"] and
            isinstance(text, dict) and
            isinstance(text.get("format"), dict) and
            text["format"].get("type") == "text" and
            text.get("verbosity") == "medium")


def _verify_saved_agent(agent_id: str, definition: dict[str, Any]) -> None:
    saved = app._api_json("GET", f"/agents/{agent_id}")
    if not isinstance(saved, dict) or saved.get("id") != agent_id:
        raise app.AppError("Saved-agent lookup returned a different ID.")
    if not _matches_definition(saved, definition):
        raise app.AppError("The saved New agent configuration differs from new_agent.json.")


def _find_matching_agent(definition: dict[str, Any]) -> str | None:
    """Adopt an identical agent after local state loss or an uncertain create POST."""
    cursor: str | None = None
    seen_cursors: set[str] = set()
    matches: list[str] = []
    for _ in range(100):
        path = "/agents?limit=100&order=desc"
        if cursor:
            path += "&after=" + quote(cursor, safe="")
        page = app._api_json("GET", path)
        data = page.get("data") if isinstance(page, dict) else None
        if not isinstance(data, list) or type(page.get("has_more")) is not bool:
            raise app.AppError("Agent listing returned an invalid page; no new agent was created.")
        for saved in data:
            if _matches_definition(saved, definition):
                agent_id = saved.get("id")
                if isinstance(agent_id, str) and re.fullmatch(r"agent_[A-Za-z0-9_-]+", agent_id):
                    matches.append(agent_id)
        if page["has_more"] is False:
            if len(matches) > 1:
                raise app.AppError("More than one saved agent matches new_agent.json. Resolve the duplicate in OpenAI Platform before starting.")
            return matches[0] if matches else None
        next_cursor = page.get("last_id")
        if not isinstance(next_cursor, str) or not next_cursor.strip() or next_cursor in seen_cursors:
            raise app.AppError("Agent listing pagination is invalid; no new agent was created.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise app.AppError("Agent listing exceeded 100 pages; no new agent was created.")


def _ensure_agent(state: dict[str, Any], definition: dict[str, Any], digest: str) -> str:
    agent_id = state.get("agent_id")
    if isinstance(agent_id, str):
        _verify_saved_agent(agent_id, definition)
        print(f"Reusing saved agent: {AGENT_NAME} ({agent_id})", file=sys.stderr)
        return agent_id
    agent_id = _find_matching_agent(definition)
    if agent_id:
        state["agent_id"] = agent_id
        state["definition_sha256"] = digest
        app.AGENT_ID = agent_id
        app._save_state(state)
        print(f"Recovered matching saved agent: {AGENT_NAME} ({agent_id})", file=sys.stderr)
        return agent_id
    created = app._api_json("POST", "/agents", definition)
    agent_id = created.get("id") if isinstance(created, dict) else None
    if not isinstance(agent_id, str) or not re.fullmatch(r"agent_[A-Za-z0-9_-]+", agent_id):
        raise app.AppError("Agent creation did not return a valid agent ID. Inspect the project before retrying to avoid a duplicate.")
    state["agent_id"] = agent_id
    state["definition_sha256"] = digest
    app.AGENT_ID = agent_id
    app._save_state(state)
    _verify_saved_agent(agent_id, definition)
    print(f"Created reusable agent: {AGENT_NAME} ({agent_id})", file=sys.stderr)
    return agent_id


def command_start(args: argparse.Namespace, state: dict[str, Any],
                  definition: dict[str, Any], digest: str) -> None:
    if state.get("pending_start"):
        raise app.PendingOutcome("A previous session start has an unknown outcome. Inspect the project's sessions in OpenAI Platform before another start. The local pending-start marker must be reconciled explicitly; it is not retried automatically.")
    agent_id = _ensure_agent(state, definition, digest)
    body = {"agent_id": agent_id, "environment": {"type": "none"},
            "input": args.message, "stream": True}
    previous_session_id = state.get("session_id")
    state["pending_start"] = {
        "id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
        "message_sha256": hashlib.sha256(args.message.encode("utf-8")).hexdigest(),
        "studio_job_id": getattr(args, "studio_job_id", None),
        "agent_id": agent_id, "project_id": app.PROJECT_ID,
        "studio_start_nonce": getattr(args, "studio_start_nonce", None),
    }
    if getattr(args, "studio_job_id", None):
        body["metadata"] = {"studio_job_id": args.studio_job_id, "studio_start_nonce": args.studio_start_nonce}
    app._save_state(state)
    process = None
    try:
        process = app._open_stream("POST", "/agents/sessions", body)
        app._consume_stream(process, state, args.raw_events, event_callback=getattr(args, "event_callback", None))
    finally:
        if process is not None:
            app._close_stream(process)
        # Only a newly observed provider session ID resolves a create ambiguity.
        if state.get("session_id") and state["session_id"] != previous_session_id:
            state.pop("pending_start", None)
            app._save_state(state)


def _message_input(args: argparse.Namespace) -> str:
    if args.message_file is not None:
        try:
            # Decode the same bytes without universal-newline conversion.
            message = args.message_file.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise app.AppError(f"Message file must contain UTF-8 text: {args.message_file}") from exc
        except OSError as exc:
            raise app.AppError(f"Cannot read message file {args.message_file}: {exc.strerror or str(exc)}") from exc
    else:
        message = args.message
    if not isinstance(message, str) or not message.strip():
        raise app.AppError("Message cannot be empty.")
    return message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and interact with New agent using curl-backed Agents HTTP calls.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="Create the reusable agent, or verify its saved ID")
    start = commands.add_parser("start", help="Create the agent if needed, then stream a new session")
    start_input = start.add_mutually_exclusive_group()
    start_input.add_argument("--message", default=DEFAULT_MESSAGE, help="Initial user message")
    start_input.add_argument("--message-file", type=Path, help="Read the initial user message from a UTF-8 text file")
    start.add_argument("--raw-events", action="store_true", help="Print full SSE JSON events to stderr")
    start.add_argument("--studio-control", action="store_true", help=argparse.SUPPRESS)
    start.add_argument("--studio-job-id", help=argparse.SUPPRESS)
    start.add_argument("--studio-start-nonce", help=argparse.SUPPRESS)
    send = commands.add_parser("send", help="Send a follow-up message to the latest session")
    send_input = send.add_mutually_exclusive_group(required=True)
    send_input.add_argument("--message", help="User message")
    send_input.add_argument("--message-file", type=Path, help="Read the user message from a UTF-8 text file")
    send.add_argument("--session-id", help="Override the saved session ID")
    send.add_argument("--idempotency-key", help="Reuse this key only when retrying the same message")
    send.add_argument("--new-submission", action="store_true",
                      help="After inspecting saved state, replace an uncertain pending message")
    send.add_argument("--raw-events", action="store_true", help="Print full SSE JSON events to stderr")
    send.add_argument("--studio-control", action="store_true", help=argparse.SUPPRESS)
    status = commands.add_parser("status", help="Retrieve session state and pending actions")
    status.add_argument("--session-id", help="Override the saved session ID")
    status.add_argument("--items", action="store_true", help="Also print the latest page of saved items")
    history = commands.add_parser("history", help="Read one bounded page of saved session items")
    history.add_argument("--session-id", required=True)
    history.add_argument("--after")
    history.add_argument("--limit", type=int, default=100)
    recovery = commands.add_parser("reconcile", help="Check a saved local submission using provider reads only")
    recovery.add_argument("--request-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in ("start", "send"):
            args.message = _message_input(args)
        if getattr(args, "studio_job_id", None) is not None and not re.fullmatch(r"[0-9a-f]{32}", args.studio_job_id):
            raise app.AppError("The local job ID is invalid.")
        if bool(getattr(args, "studio_job_id", None)) != bool(getattr(args, "studio_start_nonce", None)):
            raise app.AppError("The local start job and nonce must be supplied together.")
        if getattr(args, "studio_start_nonce", None) is not None and not re.fullmatch(r"[0-9a-f]{64}", args.studio_start_nonce):
            raise app.AppError("The local start nonce is invalid.")
        app._api_key()
        definition, digest = _definition()
        with exclusive_lock(STATE_PATH.with_suffix(".lock")):
            state = _load_profile_state(digest)
            with _bound_app(state):
                control = SessionControl(state, args.message,
                                         session_id=(args.session_id or state.get("session_id")) if args.command == "send" else None) if getattr(args, "studio_control", False) else None
                if control is not None:
                    args.event_callback = control.observe
                    control.start()
                try:
                    if args.command == "create":
                        _ensure_agent(state, definition, digest)
                    elif args.command == "start":
                        command_start(args, state, definition, digest)
                    elif args.command == "send":
                        if not state.get("agent_id"):
                            raise app.AppError("Create New agent and start a session before sending a follow-up.")
                        app.command_send(args)
                    elif args.command == "status":
                        if not state.get("agent_id"):
                            raise app.AppError("Create New agent and start a session before checking status.")
                        app.command_status(args)
                    elif args.command == "history":
                        try:
                            result = session_recovery.history(state, args.session_id, args.after, args.limit)
                        except session_recovery.Unavailable as exc:
                            raise app.AppError(str(exc)) from exc
                        print(json.dumps(result, ensure_ascii=False))
                    elif args.command == "reconcile":
                        if args.request_file.stat().st_size > 16_000:
                            raise app.AppError("The local recovery request is too large.")
                        target = json.loads(args.request_file.read_text(encoding="utf-8"))
                        if not isinstance(target, dict):
                            raise app.AppError("The local recovery request is invalid.")
                        _verify_saved_agent(state.get("agent_id"), definition)
                        print(json.dumps(session_recovery.reconcile(state, target), ensure_ascii=False))
                finally:
                    if control is not None:
                        control.finish()
        return 0
    except app.PendingOutcome as exc:
        print(f"Pending: {exc}", file=sys.stderr)
        return 2
    except (app.AppError, SessionBusy, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
