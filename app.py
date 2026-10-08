"""Small Agents API CLI. All HTTP requests are made by the curl executable."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote
import uuid
from typing import Any, BinaryIO, Callable, Iterator


PROJECT_ID = "proj_M074YvE258sr0Rg13cNmJn8j"
AGENT_ID = "agent_b37d41c0efa3435e84b94b172d10e4ed80563c721f3a4481b8"
AGENT_NAME = "SZL Estate Evidence Scout"
BASE_URL = "https://api.openai.com/v1"
ROOT = Path(__file__).resolve().parent
AGENT_PATH = ROOT / "agent.json"
STATE_PATH = ROOT / ".agent-state.json"
DEFAULT_MESSAGE = (
    "Begin a bounded, read-only public evidence scan of GitHub szl-holdings and "
    "Hugging Face SZLHOLDINGS. Identify their public entry points and a small sample "
    "of directly observed resources. Cite each finding with a direct URL, observation "
    "time, and access scope. Mark whole-estate totals UNKNOWN unless you can paginate "
    "and verify them, and state the smallest safe next public read."
)


class AppError(Exception):
    pass


class PendingOutcome(AppError):
    """The API accepted input, but the turn outcome was not observed."""


def _curl_binary() -> str:
    executable = shutil.which("curl.exe") or shutil.which("curl")
    if not executable:
        raise AppError("curl is required. Install curl or add it to PATH.")
    return executable


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise AppError("OPENAI_API_KEY is unset. Set a key for the requested OpenAI project first.")
    if key.startswith("<"):
        raise AppError("OPENAI_API_KEY is an example placeholder, not an API key. "
                       "Create a key for this OpenAI project and paste the actual secret into "
                       "your PowerShell session. Do not paste it into chat.")
    if re.fullmatch(r"\*+", key):
        raise AppError("OPENAI_API_KEY contains only masking asterisks. "
                       "Paste the actual secret from OpenAI Platform; the asterisks shown "
                       "while typing are not the key.")
    # The key is passed on curl's stdin, never as a process-list argument.
    if not re.fullmatch(r"[A-Za-z0-9._~+/\-]+=*", key):
        raise AppError("OPENAI_API_KEY contains invalid bearer-token characters. "
                       "Paste only the secret from OpenAI Platform, without quotes, labels, or spaces.")
    return key


def _curl_config(accept_events: bool = False, idempotency_key: str | None = None) -> str:
    key = _api_key()
    headers = [
        f"Authorization: Bearer {key}",
        f"OpenAI-Project: {PROJECT_ID}",
        "OpenAI-Beta: agents=v1",
        "Content-Type: application/json",
    ]
    if accept_events:
        headers.append("Accept: text/event-stream")
    if idempotency_key:
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", idempotency_key):
            raise AppError("Idempotency key contains unexpected characters.")
        headers.append(f"Idempotency-Key: {idempotency_key}")
    return "".join(f'header = "{header}"\n' for header in headers)


def _write_json_temp(directory: Path, value: Any) -> Path:
    path = directory / "request.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _error_detail(raw: bytes) -> str:
    try:
        parsed = json.loads(raw)
        error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
        if isinstance(error, dict):
            code = error.get("code") or error.get("type")
            message = error.get("message")
            if message:
                return f"{code}: {message}" if code else str(message)
    except (ValueError, TypeError):
        pass
    return raw.decode("utf-8", "replace").strip()[:2000] or "No response body"


def _api_json(method: str, path: str, body: Any = None, *, idempotency_key: str | None = None) -> Any:
    with tempfile.TemporaryDirectory(prefix="agents-api-") as temp:
        tempdir = Path(temp)
        response_path = tempdir / "response.json"
        args = [
            _curl_binary(), "--silent", "--show-error", "--fail-with-body",
            "--connect-timeout", "20", "--max-time", "90",
            "--config", "-", "--request", method,
            "--output", str(response_path), "--write-out", "%{http_code}",
        ]
        if body is not None:
            args += ["--data-binary", "@" + str(_write_json_temp(tempdir, body))]
        args.append(BASE_URL + path)
        result = subprocess.run(
            args, input=_curl_config(idempotency_key=idempotency_key), text=True,
            capture_output=True, timeout=100, check=False,
        )
        raw = response_path.read_bytes() if response_path.exists() else b""
        try:
            status = int(result.stdout.strip())
        except ValueError:
            status = 0
        if result.returncode != 0 or status >= 400 or status == 0:
            detail = _error_detail(raw)
            transport = result.stderr.strip()
            raise AppError(f"{method} {path} failed (HTTP {status}): {detail}" +
                           (f"; curl: {transport}" if transport else ""))
        try:
            return json.loads(raw) if raw else None
        except ValueError as exc:
            raise AppError(f"{method} {path} returned invalid JSON (HTTP {status}).") from exc


def _read_headers(stream: BinaryIO) -> tuple[int, dict[str, str]]:
    while True:
        first = stream.readline()
        if not first:
            raise AppError("The event stream closed before HTTP headers arrived.")
        status_line = first.decode("iso-8859-1", "replace").strip()
        if not status_line.startswith("HTTP/"):
            raise AppError(f"Unexpected HTTP response: {status_line[:200]}")
        try:
            status = int(status_line.split()[1])
        except (ValueError, IndexError) as exc:
            raise AppError(f"Invalid HTTP status line: {status_line[:200]}") from exc
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            name, separator, value = line.decode("iso-8859-1", "replace").partition(":")
            if separator:
                headers[name.lower()] = value.strip()
        # A proxy CONNECT response may precede the actual API response.
        if 100 <= status < 200 or "connection established" in status_line.lower():
            continue
        return status, headers


def _open_stream(method: str, path: str, body: Any = None) -> subprocess.Popen[bytes]:
    # A request body lives only until curl has opened it. The caller closes the process.
    tempdir = tempfile.TemporaryDirectory(prefix="agents-api-stream-")
    args = [
        _curl_binary(), "--silent", "--show-error", "--fail-with-body",
        "--no-buffer", "--connect-timeout", "20", "--max-time", "1800",
        "--dump-header", "-", "--config", "-", "--request", method,
    ]
    if body is not None:
        args += ["--data-binary", "@" + str(_write_json_temp(Path(tempdir.name), body))]
    args.append(BASE_URL + path)
    try:
        process = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(_curl_config(accept_events=True).encode("utf-8"))
        process.stdin.close()
        status, headers = _read_headers(process.stdout)
        if status >= 400:
            raw = process.stdout.read(64 * 1024)
            process.wait(timeout=10)
            raise AppError(f"{method} {path} failed (HTTP {status}): {_error_detail(raw)}")
        if not 200 <= status < 300:
            raise AppError(f"{method} {path} returned unexpected HTTP {status}.")
        if "text/event-stream" not in headers.get("content-type", ""):
            raise AppError(f"{method} {path} returned {headers.get('content-type', 'no content type')}, not an event stream.")
        # Keep TemporaryDirectory alive until curl closes its request file.
        process._agents_tempdir = tempdir  # type: ignore[attr-defined]
        return process
    except Exception:
        tempdir.cleanup()
        if "process" in locals():
            process.kill()
            process.wait()
        raise


def _close_stream(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    if process.stdout:
        process.stdout.close()
    if process.stderr:
        process.stderr.close()
    tempdir = getattr(process, "_agents_tempdir", None)
    if tempdir:
        tempdir.cleanup()


def _sse_events(stream: BinaryIO) -> Iterator[dict[str, Any]]:
    data: list[str] = []
    while True:
        raw = stream.readline()
        if not raw:
            if data:
                yield _decode_sse(data)
            return
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                yield _decode_sse(data)
                data.clear()
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))


def _decode_sse(lines: list[str]) -> dict[str, Any]:
    payload = "\n".join(lines)
    if payload == "[DONE]":
        return {"type": "[DONE]"}
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise AppError(f"Invalid SSE event JSON: {payload[:200]}") from exc
    if not isinstance(event, dict):
        raise AppError("SSE event must be a JSON object.")
    return event


def _fresh_turn_events(events: Iterator[dict[str, Any]], previous_turn_id: str | None,
                       filter_state: dict[str, bool]
                       ) -> Iterator[dict[str, Any]]:
    """Ignore the preceding turn; do not attribute ID-less early events to a new turn."""
    target_turn_id: str | None = None
    for event in events:
        kind = event.get("type")
        if not isinstance(kind, str) or not kind.startswith("agent.session.turn."):
            yield event
            continue
        turn_id = _event_turn_id(event)
        if turn_id and turn_id == previous_turn_id:
            continue
        if target_turn_id is None:
            if turn_id is None:
                filter_state["dropped_ambiguous"] = True
                continue
            target_turn_id = turn_id
        if turn_id is None or turn_id == target_turn_id:
            yield event


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"project_id": PROJECT_ID}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AppError(f"Invalid state file: {STATE_PATH}") from exc
    if not isinstance(state, dict) or state.get("project_id") != PROJECT_ID:
        raise AppError(f"State file belongs to a different project: {STATE_PATH}")
    if state.get("agent_id") not in (None, AGENT_ID):
        raise AppError(f"State file belongs to a different saved agent: {STATE_PATH}")
    return state


def _save_state(state: dict[str, Any]) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, STATE_PATH)


def _agent_overrides() -> dict[str, Any]:
    try:
        agent = json.loads(AGENT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AppError(f"Cannot read session overrides: {AGENT_PATH}") from exc
    expected = {"model", "instructions", "reasoning", "text"}
    if not isinstance(agent, dict) or set(agent) != expected:
        raise AppError("agent.json must contain exactly model, instructions, reasoning, "
                       "and text so the saved agent's other settings are preserved.")
    if not isinstance(agent["model"], str) or not agent["model"]:
        raise AppError("agent.json model must be a nonempty string.")
    if not isinstance(agent["instructions"], str) or not agent["instructions"]:
        raise AppError("agent.json instructions must be a nonempty string.")
    if not isinstance(agent["reasoning"], dict) or set(agent["reasoning"]) != {"effort", "summary"}:
        raise AppError("agent.json reasoning must contain effort and summary.")
    if not isinstance(agent["text"], dict) or set(agent["text"]) != {"format", "verbosity"}:
        raise AppError("agent.json text must contain format and verbosity.")
    return agent


def _verify_saved_agent() -> None:
    saved = _api_json("GET", f"/agents/{AGENT_ID}")
    if not isinstance(saved, dict) or saved.get("id") != AGENT_ID:
        raise AppError("The saved agent lookup did not return the requested agent ID.")
    if saved.get("name") != AGENT_NAME:
        raise AppError(f"Saved agent name is {saved.get('name')!r}; expected {AGENT_NAME!r}. "
                       "Check project access and the agent ID before starting a session.")
    tools = saved.get("tools")
    if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict) or tools[0].get("type") != "web_search":
        raise AppError("The saved agent must have only the web_search tool for this "
                       "read-only runner. Inspect the saved agent configuration first.")
    multi_agent = saved.get("multi_agent")
    if isinstance(multi_agent, dict) and multi_agent.get("enabled") is True:
        raise AppError("The saved agent has multi_agent enabled; the supplied configuration "
                       "expects it disabled. Inspect the saved agent before starting.")


def _event_session_id(event: dict[str, Any]) -> str | None:
    session = event.get("session")
    if isinstance(session, dict) and isinstance(session.get("id"), str):
        return session["id"]
    value = event.get("session_id")
    return value if isinstance(value, str) else None


def _event_turn_id(event: dict[str, Any]) -> str | None:
    value = event.get("turn_id")
    if isinstance(value, str):
        return value
    turn = event.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return turn["id"]
    item = event.get("item")
    if isinstance(item, dict) and isinstance(item.get("turn_id"), str):
        return item["turn_id"]
    return None


def _required_actions(event: dict[str, Any], session_id: str | None) -> list[Any]:
    session = event.get("session")
    actions = session.get("required_actions") if isinstance(session, dict) else None
    if actions is None and session_id:
        saved = _api_json("GET", f"/agents/sessions/{session_id}")
        actions = saved.get("required_actions") if isinstance(saved, dict) else None
    return actions if isinstance(actions, list) else []


def _tool_issue(item: dict[str, Any]) -> str | None:
    if str(item.get("type", "")).endswith("_call"):
        if item.get("status") != "completed" or item.get("error"):
            return (f"{item.get('type')} {item.get('id', '')}: "
                    f"status={item.get('status', 'unknown')}, "
                    f"error={item.get('error') or 'none'}")
    return None


def _raise_tool_issues(issues: list[str]) -> None:
    if issues:
        raise AppError("One or more tool calls did not complete successfully; "
                       "treat the answer as degraded and inspect `status --items`: "
                       + "; ".join(issues))


def _print_event(event: dict[str, Any], seen_text: dict[tuple[Any, Any, Any], str],
                 raw_events: bool, suppress_stdout: bool = False) -> None:
    if raw_events:
        print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
    kind = event.get("type", "unknown")
    key = (event.get("item_id"), event.get("output_index"), event.get("content_index"))
    if kind == "agent.session.turn.output_text.delta":
        delta = event.get("delta", "")
        if isinstance(delta, str):
            seen_text[key] = seen_text.get(key, "") + delta
            if not raw_events:
                print(f"[text delta] {delta}", file=sys.stderr, flush=True)
        return
    if kind == "agent.session.turn.output_text.done":
        full = event.get("text", "")
        if isinstance(full, str):
            seen_text[key] = full
            # Deltas are provisional; only the authoritative done text goes to stdout.
            if not suppress_stdout:
                print(full, end="" if full.endswith("\n") else "\n", flush=True)
        return
    if not raw_events:
        print(f"[{kind}]", file=sys.stderr, flush=True)
    item = event.get("item")
    if isinstance(item, dict) and (str(item.get("type", "")).endswith("_call") or item.get("type") == "function_call"):
        print(f"  tool={item.get('type')} status={item.get('status', 'unknown')} id={item.get('id', '')}",
              file=sys.stderr, flush=True)
        if item.get("error"):
            print(f"  tool_error={item['error']}", file=sys.stderr, flush=True)


def _consume_stream(process: subprocess.Popen[bytes], state: dict[str, Any], raw_events: bool,
                    previous_turn_id: str | None = None, require_fresh_turn: bool = False,
                    event_callback: Callable[[dict[str, Any]], None] | None = None) -> None:
    assert process.stdout is not None
    seen_text: dict[tuple[Any, Any, Any], str] = {}
    completed = False
    completed_turn_id: str | None = None
    tool_issues: list[str] = []
    filter_state = {"dropped_ambiguous": False}
    events = _sse_events(process.stdout)
    if require_fresh_turn:
        events = _fresh_turn_events(events, previous_turn_id, filter_state)
    for event in events:
        kind = event.get("type")
        turn_id = _event_turn_id(event)
        session_id = _event_session_id(event)
        if session_id and state.get("session_id") != session_id:
            state["session_id"] = session_id
            _save_state(state)
            print(f"Session ID: {session_id}", file=sys.stderr, flush=True)
        if event_callback is not None:
            event_callback(event)
        _print_event(event, seen_text, raw_events,
                     suppress_stdout=filter_state["dropped_ambiguous"])
        item = event.get("item")
        if kind == "agent.session.turn.item.done" and isinstance(item, dict):
            issue = _tool_issue(item)
            if issue:
                tool_issues.append(issue)
        if kind == "agent.session.requires_action":
            actions = _required_actions(event, state.get("session_id"))
            print("Pending actions: " + json.dumps(actions, ensure_ascii=False), file=sys.stderr)
            raise AppError("The session needs an application action. This agent only defines built-in web_search; no local function handler is configured.")
        if kind == "error":
            error = event.get("error")
            detail = error.get("message") if isinstance(error, dict) else error
            raise AppError(f"Agent stream error: {detail or 'unknown error'}")
        if kind in ("agent.session.failed", "agent.session.environment.failed"):
            raise AppError(f"Agent lifecycle failure: {kind}; inspect `status`.")
        if kind in ("agent.session.turn.completed", "agent.session.turn.failed", "agent.session.turn.cancelled"):
            turn = event.get("turn")
            if isinstance(turn, dict) and turn.get("subagent_id") is not None:
                continue
            if kind == "agent.session.turn.completed":
                completed = True
                completed_turn_id = turn_id
                if turn_id:
                    state["last_turn_id"] = turn_id
                    _save_state(state)
                break
            error = turn.get("error") if isinstance(turn, dict) else None
            detail = error.get("message") if isinstance(error, dict) else error
            raise AppError(f"Root turn ended with {kind}: {detail or 'no detail'}")
    if not completed:
        stderr = process.stderr.read().decode("utf-8", "replace").strip() if process.stderr else ""
        raise PendingOutcome("Event stream closed before the root turn completed. "
                             "Run `status --items` to inspect saved state."
                             + (f" curl: {stderr}" if stderr else ""))
    if filter_state["dropped_ambiguous"]:
        session_id = state.get("session_id")
        if not isinstance(session_id, str) or not completed_turn_id:
            raise PendingOutcome("The turn completed, but its ID was unavailable for saved-output recovery.")
        items = _turn_items(session_id, completed_turn_id)
        replies = _saved_final_replies(items)
        tool_issues.extend(issue for item in items if (issue := _tool_issue(item)))
        for reply in replies:
            print(reply, end="" if reply.endswith("\n") else "\n", flush=True)
        print("Recovered the completed final answer from saved items after ambiguous early events.",
              file=sys.stderr)
    _raise_tool_issues(tool_issues)


def _session_id(args: argparse.Namespace, state: dict[str, Any]) -> str:
    value = args.session_id or state.get("session_id")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AppError("A session ID is required. Run `start` or pass --session-id.")
    return value


def _latest_turn(session_id: str) -> dict[str, Any] | None:
    turns = _api_json("GET", f"/agents/sessions/{session_id}/turns?order=desc&limit=1")
    data = turns.get("data") if isinstance(turns, dict) else None
    if not isinstance(data, list):
        raise AppError("The turns endpoint returned no data list.")
    return data[0] if data and isinstance(data[0], dict) else None


def _turn_items(session_id: str, turn_id: str) -> list[dict[str, Any]]:
    """Walk newest-first saved items until the target turn's user input is found."""
    items: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(100):
        path = f"/agents/sessions/{session_id}/items?order=desc&limit=100"
        if after:
            path += "&after=" + quote(after, safe="")
        page = _api_json("GET", path)
        data = page.get("data") if isinstance(page, dict) else None
        if not isinstance(data, list):
            raise AppError("The items endpoint returned no data list.")
        for item in data:
            if isinstance(item, dict) and item.get("turn_id") == turn_id:
                items.append(item)
        if any(item.get("type") == "message" and item.get("role") == "user" for item in items):
            return list(reversed(items))
        if not page.get("has_more"):
            return list(reversed(items))
        cursor = page.get("last_id")
        if not isinstance(cursor, str) or not cursor or cursor == after:
            raise AppError("Items pagination ended without a valid next cursor.")
        after = cursor
    raise PendingOutcome("Too many saved item pages to reconcile automatically. Run `status --items`.")


def _item_text(item: dict[str, Any], part_type: str) -> list[str]:
    content = item.get("content")
    if not isinstance(content, list):
        return []
    return [part["text"] for part in content if isinstance(part, dict)
            and part.get("type") == part_type and isinstance(part.get("text"), str)]


def _saved_final_replies(items: list[dict[str, Any]]) -> list[str]:
    final_items = [item for item in items if item.get("type") == "message"
                   and item.get("role") == "assistant" and item.get("phase") == "final_answer"]
    if not final_items or any(item.get("status") != "completed" for item in final_items):
        raise PendingOutcome("The turn completed, but its saved final answer is not complete yet. "
                             "Run `status --items` and retry later.")
    replies = [part for item in final_items for part in _item_text(item, "output_text")]
    if not replies:
        raise PendingOutcome("The turn completed, but its saved final answer is not available yet. "
                             "Run `status --items` and retry later.")
    return replies


def _reconcile_pending_turn(state: dict[str, Any], session_id: str, message: str,
                            turn: dict[str, Any]) -> None:
    turn_id = turn.get("id")
    if not isinstance(turn_id, str) or not turn_id:
        raise PendingOutcome("The latest turn has no ID. Run `status --items`.")
    items = _turn_items(session_id, turn_id)
    if not any(message in _item_text(item, "input_text") for item in items
               if item.get("type") == "message" and item.get("role") == "user"):
        raise PendingOutcome("A newer turn exists, but its saved user input does not match the pending message. "
                             "Run `status --items` before sending again.")
    status = turn.get("status")
    if status == "completed":
        replies = _saved_final_replies(items)
        for reply in replies:
            print(reply, end="" if reply.endswith("\n") else "\n", flush=True)
        _raise_tool_issues([issue for item in items if (issue := _tool_issue(item))])
        state["last_turn_id"] = turn_id
        state["pending_submissions"].pop(session_id, None)
        _save_state(state)
        print(f"Recovered completed turn {turn_id} from saved items.", file=sys.stderr)
        return
    if status in ("failed", "cancelled"):
        error = turn.get("error")
        detail = error.get("message") if isinstance(error, dict) else error
        raise AppError(f"Pending message belongs to turn {turn_id}, which {status}: "
                       f"{detail or 'no detail'}")
    raise PendingOutcome(f"Pending message belongs to turn {turn_id}, which is {status or 'unknown'}. "
                         "Run `status --items` to inspect it.")


def _submission_record(state: dict[str, Any], session_id: str, message: str,
                       requested_key: str | None, new_submission: bool,
                       previous_turn_id: str | None = None) -> tuple[str, bool]:
    pending = state.get("pending_submissions", {})
    if not isinstance(pending, dict):
        raise AppError("Invalid pending submissions in state file.")
    previous = pending.get(session_id)
    if previous and not new_submission:
        if not isinstance(previous, dict) or previous.get("message") != message:
            raise AppError("A previous message may still be pending. Run `status --items` first, "
                           "then pass --new-submission if you intend to send a different message.")
        key = previous.get("idempotency_key")
        if not isinstance(key, str) or (requested_key and requested_key != key):
            raise AppError("The retry key does not match the saved pending message. "
                           "Use its saved key or inspect `status --items` before --new-submission.")
        return key, True
    key = requested_key or str(uuid.uuid4())
    # Validate before saving and sending it through curl's config stdin.
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", key):
        raise AppError("Idempotency key contains unexpected characters.")
    pending[session_id] = {"message": message, "idempotency_key": key,
                           "previous_turn_id": previous_turn_id}
    state["pending_submissions"] = pending
    _save_state(state)
    return key, False


def command_start(args: argparse.Namespace) -> None:
    state = _load_state()
    agent = _agent_overrides()
    _verify_saved_agent()
    print(f"Verified saved agent: {AGENT_NAME} ({AGENT_ID})", file=sys.stderr)
    body = {"agent_id": AGENT_ID, "agent": agent,
            "environment": {"type": "none"}, "input": args.message, "stream": True}
    state["agent_id"] = AGENT_ID
    process = _open_stream("POST", "/agents/sessions", body)
    try:
        _consume_stream(process, state, args.raw_events)
    finally:
        _close_stream(process)


def command_send(args: argparse.Namespace) -> None:
    state = _load_state()
    session_id = _session_id(args, state)
    path = f"/agents/sessions/{session_id}/events"
    body = {"events": [{"type": "agent.session.input.message", "input": [{
        "role": "user", "content": [{"type": "input_text", "text": args.message}]
    }]}]}
    # A repeat after an uncertain POST uses the exact saved key and message.
    pending = state.get("pending_submissions", {})
    if not isinstance(pending, dict):
        raise AppError("Invalid pending submissions in state file.")
    existing = pending.get(session_id)
    if existing and not args.new_submission:
        key, _ = _submission_record(state, session_id, args.message,
                                    args.idempotency_key, False)
        previous_turn_id = existing.get("previous_turn_id")
        if not isinstance(previous_turn_id, str):
            previous_turn_id = state.get("last_turn_id")
        latest = _latest_turn(session_id)
        if latest and latest.get("id") != previous_turn_id:
            _reconcile_pending_turn(state, session_id, args.message, latest)
            return
        process = _open_stream("GET", path + "?stream=true")
        try:
            _api_json("POST", path, body, idempotency_key=key)
            print(f"Retried the saved message with Idempotency-Key: {key}", file=sys.stderr)
            _consume_stream(process, state, args.raw_events,
                            previous_turn_id, require_fresh_turn=True,
                            event_callback=getattr(args, "event_callback", None))
            pending.pop(session_id, None)
            _save_state(state)
            return
        finally:
            _close_stream(process)
    session = _api_json("GET", f"/agents/sessions/{session_id}")
    session_status = session.get("status") if isinstance(session, dict) else None
    if session_status != "idle":
        raise AppError(f"Session {session_id} is {session_status or 'unknown'}, not ready for a new turn. "
                       "Run `status --items` to inspect it.")
    latest = _latest_turn(session_id)
    previous_turn_id = latest.get("id") if latest else None
    # Subscribe before a new submission so the turn's early events are visible.
    process = _open_stream("GET", path + "?stream=true")
    try:
        key, _ = _submission_record(state, session_id, args.message,
                                    args.idempotency_key, args.new_submission,
                                    previous_turn_id)
        print(f"Saved message and Idempotency-Key: {key}", file=sys.stderr)
        _api_json("POST", path, body, idempotency_key=key)
        print(f"Submitted message (Idempotency-Key: {key})", file=sys.stderr)
        _consume_stream(process, state, args.raw_events, previous_turn_id, require_fresh_turn=True,
                        event_callback=getattr(args, "event_callback", None))
        state.get("pending_submissions", {}).pop(session_id, None)
        _save_state(state)
    except AppError:
        print("If submission was uncertain, retry the same message to reuse its saved key. "
              "Run `status --items` to inspect saved work.", file=sys.stderr)
        raise
    finally:
        _close_stream(process)


def command_status(args: argparse.Namespace) -> None:
    state = _load_state()
    session_id = _session_id(args, state)
    session = _api_json("GET", f"/agents/sessions/{session_id}")
    print(json.dumps(session, indent=2, ensure_ascii=False))
    latest = _latest_turn(session_id)
    print(json.dumps({"latest_turn": latest},
                     indent=2, ensure_ascii=False))
    if args.items:
        items = _api_json("GET", f"/agents/sessions/{session_id}/items?order=desc&limit=100")
        print(json.dumps(items, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and interact with an OpenAI Agents API session using curl.")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Start a streamed session from the saved agent")
    start.add_argument("--message", default=DEFAULT_MESSAGE, help="Initial user message")
    start.add_argument("--raw-events", action="store_true", help="Print full SSE JSON events to stderr")
    send = commands.add_parser("send", help="Send a follow-up message to the saved session")
    send.add_argument("--message", required=True, help="User message")
    send.add_argument("--session-id", help="Override the saved session ID")
    send.add_argument("--idempotency-key", help="Reuse this key only when retrying the same message")
    send.add_argument("--new-submission", action="store_true",
                      help="After inspecting saved state, replace an uncertain pending message")
    send.add_argument("--raw-events", action="store_true", help="Print full SSE JSON events to stderr")
    status = commands.add_parser("status", help="Retrieve session state and pending actions")
    status.add_argument("--session-id", help="Override the saved session ID")
    status.add_argument("--items", action="store_true", help="Also print the latest page of saved items")
    args = parser.parse_args(argv)
    try:
        _api_key()
        if args.command in ("start", "send") and not args.message.strip():
            raise AppError("Message cannot be empty.")
        {"start": command_start, "send": command_send, "status": command_status}[args.command](args)
        return 0
    except PendingOutcome as exc:
        print(f"Pending: {exc}", file=sys.stderr)
        return 2
    except (AppError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
