"""Bounded provider readback. Recovery never submits or cancels provider work."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlencode

import app


IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,200}")
CURSOR = re.compile(r"[A-Za-z0-9_.:~+/=-]{1,512}")
DIGEST = re.compile(r"[0-9a-f]{64}")
START_WINDOW_SECONDS = 1800
CLOCK_SKEW_SECONDS = 5
MAX_PAGES = 10
MAX_READS = 64
MAX_RECOVERY_BYTES = 750_000


class Unavailable(Exception):
    pass


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise Unavailable("A required provider identity is missing or invalid.")
    return value


def timestamp(value: Any) -> float:
    if not isinstance(value, str):
        raise Unavailable("The saved operation timestamp is unavailable.")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except ValueError as exc:
        raise Unavailable("The saved operation timestamp is invalid.") from exc


class Readback:
    def __init__(self) -> None:
        self.reads = 0
        self.deadline = time.monotonic() + 180

    def get(self, path: str) -> Any:
        if self.reads >= MAX_READS or time.monotonic() >= self.deadline:
            raise Unavailable("The bounded provider readback limit was reached; no pending marker was cleared.")
        self.reads += 1
        return app._api_json("GET", path)

    def page(self, path: str, *, after: str | None = None, limit: int = 100,
             order: str = "desc", extra: dict[str, str] | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise Unavailable("History limit must be an integer from 1 to 100.")
        if after is not None and (not isinstance(after, str) or not CURSOR.fullmatch(after)):
            raise Unavailable("The history cursor is invalid.")
        query: dict[str, Any] = {"order": order, "limit": limit, **(extra or {})}
        if after is not None:
            query["after"] = after
        page = self.get(path + "?" + urlencode(query))
        if (not isinstance(page, dict) or not isinstance(page.get("data"), list)
                or type(page.get("has_more")) is not bool or len(page["data"]) > limit):
            raise Unavailable("The provider returned an invalid page.")
        ids = [identifier(item.get("id")) for item in page["data"] if isinstance(item, dict)]
        if len(ids) != len(page["data"]) or len(set(ids)) != len(ids):
            raise Unavailable("The provider returned invalid or duplicate item identities.")
        for field in ("first_id", "last_id"):
            cursor = page.get(field)
            if cursor is not None and (not isinstance(cursor, str) or not CURSOR.fullmatch(cursor)):
                raise Unavailable("The provider returned an invalid page cursor.")
        if page["has_more"] and (not page["data"] or not page.get("last_id") or page["last_id"] == after):
            raise Unavailable("The provider page did not advance its cursor.")
        return page

    def all_pages(self, path: str, *, order: str = "asc") -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        after = None
        for _ in range(MAX_PAGES):
            page = self.page(path, after=after, order=order)
            for item in page["data"]:
                if item["id"] in seen_ids:
                    raise Unavailable("Provider pagination repeated an item; recovery is incomplete.")
                seen_ids.add(item["id"])
                data.append(item)
            if not page["has_more"]:
                return data
            after = page["last_id"]
            if after in seen_cursors:
                raise Unavailable("Provider pagination repeated a cursor; recovery is incomplete.")
            seen_cursors.add(after)
        raise Unavailable("Provider history exceeded the bounded page limit; recovery is incomplete.")


def verified_session(read: Readback, session_id: str, agent_id: str) -> dict[str, Any]:
    session = read.get(f"/agents/sessions/{identifier(session_id)}")
    if (not isinstance(session, dict) or session.get("id") != session_id
            or not isinstance(session.get("agent"), dict) or session["agent"].get("id") != agent_id
            or session.get("project_id", app.PROJECT_ID) != app.PROJECT_ID):
        raise Unavailable("The provider session does not match this saved agent and project.")
    return session


def history(state: dict[str, Any], session_id: str, after: str | None = None,
            limit: int = 100) -> dict[str, Any]:
    if (type(limit) is not int or not 1 <= limit <= 100 or
            (after is not None and (not isinstance(after, str) or not CURSOR.fullmatch(after)))):
        raise Unavailable("Use a valid cursor and integer limit from 1 to 100.")
    read = Readback()
    verified_session(read, session_id, identifier(state.get("agent_id")))
    page = read.page(f"/agents/sessions/{session_id}/items", after=after, limit=limit)
    return {"session_id": session_id, "items": page, "after": after, "limit": limit,
            "order": "desc", "next_cursor": page.get("last_id") if page["has_more"] else None}


def verified_turn(read: Readback, session_id: str, turn_id: str, agent_id: str) -> dict[str, Any]:
    turn = read.get(f"/agents/sessions/{identifier(session_id)}/turns/{identifier(turn_id)}")
    if (not isinstance(turn, dict) or turn.get("id") != turn_id
            or turn.get("session_id") != session_id or turn.get("agent_id") != agent_id
            or turn.get("subagent_id") is not None):
        raise Unavailable("The provider turn does not match the exact root session and agent.")
    return turn


def turn_items(read: Readback, session_id: str, turn_id: str) -> list[dict[str, Any]]:
    items = read.all_pages(f"/agents/sessions/{session_id}/turns/{turn_id}/items")
    if any(item.get("turn_id") != turn_id or item.get("subagent_id") is not None for item in items):
        raise Unavailable("The saved items do not belong to the exact root turn.")
    return items


def input_matches(items: list[dict[str, Any]], digest: str) -> bool:
    users = [item for item in items if item.get("type") == "message" and item.get("role") == "user"]
    if len(users) != 1:
        return False
    parts = app._item_text(users[0], "input_text")
    return len(parts) == 1 and hashlib.sha256(parts[0].encode("utf-8")).hexdigest() == digest


def start_candidates(read: Readback, state: dict[str, Any], target: dict[str, Any],
                     current_time: float) -> list[tuple[str, str]]:
    marker = state.get("pending_start")
    if not isinstance(marker, dict) or marker.get("message_sha256") != target["message_sha256"]:
        raise Unavailable("No exact pending-start marker is available for this job.")
    nonce = target.get("studio_start_nonce")
    if (not isinstance(nonce, str) or not DIGEST.fullmatch(nonce)
            or marker.get("studio_start_nonce") != nonce or marker.get("studio_job_id") != target["id"]):
        raise Unavailable("The original start has no exact provider metadata binding. Legacy unobserved starts require manual inspection; no marker was cleared.")
    created = timestamp(marker.get("created_at"))
    if marker.get("agent_id", state.get("agent_id")) != state.get("agent_id") or marker.get("project_id", app.PROJECT_ID) != app.PROJECT_ID:
        raise Unavailable("The pending-start marker belongs to another saved agent or project.")
    if created < timestamp(target.get("created_at")):
        raise Unavailable("The job and pending-start timestamps do not agree.")
    lower, upper = created - CLOCK_SKEW_SECONDS, created + START_WINDOW_SECONDS
    if current_time < lower:
        raise Unavailable("The saved start timestamp is in the future; no marker was cleared.")
    agent_id = identifier(state.get("agent_id"))
    candidates: list[dict[str, Any]] = []
    after = None
    previous_time = float("inf")
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    complete = False
    for _ in range(MAX_PAGES):
        page = read.page("/agents/sessions", after=after, extra={"agent_id": agent_id})
        for session in page["data"]:
            sid = session["id"]
            made = session.get("created_at")
            if (sid in seen_ids or type(made) is not int or made > previous_time
                    or not isinstance(session.get("agent"), dict) or session["agent"].get("id") != agent_id
                    or session.get("project_id", app.PROJECT_ID) != app.PROJECT_ID):
                raise Unavailable("The candidate session listing is inconsistent; no marker was cleared.")
            seen_ids.add(sid)
            previous_time = made
            metadata = session.get("metadata")
            if (lower <= made <= upper and isinstance(metadata, dict)
                    and metadata.get("studio_start_nonce") == nonce and metadata.get("studio_job_id") == target["id"]):
                candidates.append(session)
            if made < lower:
                complete = True
        if complete or not page["has_more"]:
            complete = True
            break
        after = page["last_id"]
        if after in seen_cursors:
            raise Unavailable("Candidate pagination repeated a cursor.")
        seen_cursors.add(after)
    if not complete or len(candidates) > 20:
        raise Unavailable("The candidate session set could not be completely checked within the recovery bound.")
    matches = []
    for candidate in candidates:
        sid = candidate["id"]
        first = read.page(f"/agents/sessions/{sid}/turns", order="asc", limit=1)
        if not first["data"]:
            raise Unavailable("A candidate's first turn is not visible yet; uniqueness is not established.")
        tid = first["data"][0]["id"]
        verified_turn(read, sid, tid, agent_id)
        items = turn_items(read, sid, tid)
        if not any(item.get("role") == "user" for item in items):
            raise Unavailable("A candidate's initial input is not visible yet; uniqueness is not established.")
        if input_matches(items, target["message_sha256"]):
            matches.append((sid, tid))
    return matches


def reconcile(state: dict[str, Any], target: dict[str, Any], *, current_time: float | None = None) -> dict[str, Any]:
    result = {"target_job_id": target.get("id"), "outcome": "unavailable", "target_status": "pending",
              "session_id": target.get("session_id"), "turn_id": target.get("root_turn_id"),
              "detail": "The exact provider outcome is not available.", "pending_resolved": False}
    try:
        if (state.get("project_id") != app.PROJECT_ID or target.get("action") not in ("start", "send")
                or not isinstance(target.get("message_sha256"), str) or not DIGEST.fullmatch(target["message_sha256"])):
            raise Unavailable("The saved job's original submission binding is unavailable.")
        agent_id = identifier(state.get("agent_id"))
        read = Readback()
        sid, tid = target.get("session_id"), target.get("root_turn_id")
        saved_resolution = state.get("resolved_submissions", {}).get(target.get("client_submission_id") or target.get("id"))
        if isinstance(saved_resolution, dict) and saved_resolution.get("message_sha256") == target["message_sha256"]:
            if sid not in (None, saved_resolution.get("session_id")) or tid not in (None, saved_resolution.get("turn_id")):
                raise Unavailable("The durable reconciliation disagrees with the original observed identities.")
            sid, tid = saved_resolution.get("session_id"), saved_resolution.get("turn_id")
        if sid is None:
            if target["action"] != "start":
                raise Unavailable("The original session identity was not observed.")
            matches = start_candidates(read, state, target, current_time or datetime.now(timezone.utc).timestamp())
            if len(matches) != 1:
                result.update(outcome="ambiguous" if matches else "not_found",
                              detail="Multiple exact candidates were found." if matches else "No exact candidate was found. Absence does not prove the original start failed.")
                return result
            sid, tid = matches[0]
        session = verified_session(read, identifier(sid), agent_id)
        if target["action"] == "start" and target.get("session_id") is None:
            metadata = session.get("metadata")
            if (not isinstance(metadata, dict) or metadata.get("studio_start_nonce") != target.get("studio_start_nonce")
                    or metadata.get("studio_job_id") != target["id"]):
                raise Unavailable("The retrieved session no longer has the original start metadata binding.")
        pending = state.get("pending_submissions", {}).get(sid)
        if tid is None:
            if target["action"] == "start":
                first = read.page(f"/agents/sessions/{sid}/turns", order="asc", limit=1)
                if not first["data"]:
                    raise Unavailable("The first root turn is not visible.")
                tid = first["data"][0]["id"]
            else:
                raise Unavailable("The original follow-up root turn was not observed. The provider does not echo its idempotency key in saved turns; matching text alone cannot establish identity.")
        turn = verified_turn(read, sid, identifier(tid), agent_id)
        items = turn_items(read, sid, tid)
        if not input_matches(items, target["message_sha256"]):
            raise Unavailable("The exact turn's saved user input does not match the original submission hash.")
        result.update(session_id=sid, turn_id=tid)
        status = turn.get("status")
        if status not in ("completed", "failed", "cancelled"):
            result.update(outcome="pending", detail="The exact matched root turn is still nonterminal; no work was resent.")
            return result
        text = "\n".join(part for item in items if item.get("type") == "message" and item.get("role") == "assistant"
                         for part in app._item_text(item, "output_text"))
        if status == "completed":
            text = "\n".join(app._saved_final_replies(items))
            issues = [issue for item in items if (issue := app._tool_issue(item))]
        else:
            issues = []
        latest = read.page(f"/agents/sessions/{sid}/turns", limit=1)
        snapshot = {"session": session, "latest_turn": latest["data"][0] if latest["data"] else None,
                    "items": read.page(f"/agents/sessions/{sid}/items")}
        if len(json.dumps({"text": text, "snapshot": snapshot}).encode("utf-8")) > MAX_RECOVERY_BYTES:
            raise Unavailable("The provider readback exceeds the local recovery result bound. Use paginated history; the marker is retained.")
        updated = deepcopy(state)
        marker = updated.get("pending_start")
        if target["action"] == "start" and marker:
            if (marker.get("message_sha256") != target["message_sha256"]
                    or marker.get("studio_job_id") not in (None, target["id"])):
                raise Unavailable("A different pending start now exists; its marker was preserved.")
            updated.pop("pending_start")
        if target["action"] == "send" and pending:
            pending_hash = hashlib.sha256(str(pending.get("message", "")).encode("utf-8")).hexdigest()
            if pending_hash != target["message_sha256"] or pending.get("idempotency_key") != target.get("client_submission_id"):
                raise Unavailable("A different pending submission exists; its marker was preserved.")
            updated["pending_submissions"].pop(sid)
        resolved = updated.setdefault("resolved_submissions", {})
        resolved[target.get("client_submission_id") or target["id"]] = {
            "session_id": sid, "turn_id": tid, "message_sha256": target["message_sha256"], "status": status,
            "evidence_class": "MEASURED", "observed_at": datetime.now(timezone.utc).isoformat(),
            "scope": "Local readback reconciliation; unsigned; provider GET requests only"}
        if target["action"] == "start" and marker:
            updated["session_id"] = sid
        app._save_state(updated)
        result.update(outcome="resolved", target_status="failed" if issues else status,
                      provider_terminal_status=status, detail="Exact provider turn and original input were read back; no generation was submitted.",
                      pending_resolved=True, text=text, partial=status != "completed", snapshot=snapshot,
                      tool_issues=issues)
        return result
    except (Unavailable, app.PendingOutcome) as exc:
        result["detail"] = str(exc)
    except Exception as exc:
        result["detail"] = f"Provider readback or local persistence is unavailable ({type(exc).__name__}); no automatic retry was made."
    return result
