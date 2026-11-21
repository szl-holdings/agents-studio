"""Offline, source-pinned SZL proposer/skeptic peer-coding pact.

This is NOT an A2A implementation, a model runner, an authorizer, or a proof
that a reported test actually ran. No model/provider calls or arbitrary shell
execution. Only the CLI's fixed Git source readback is executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

TASK = "szl.peer-pact.task/v1"
PROPOSAL = "szl.peer-pact.proposal/v1"
REVIEW = "szl.peer-pact.challenge/v1"
TESTS = "szl.peer-pact.tests/v1"
RECEIPT = "szl.peer-pact.receipt/v1"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")
ACTOR = re.compile(r"[a-z][a-z0-9_-]{2,31}\Z")
TASK_ID = re.compile(r"[a-z0-9][a-z0-9-]{3,47}\Z")
TEST_ID = re.compile(r"[a-z][a-z0-9_-]{2,47}\Z")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
MAX_BYTES = 131072
MAX_FILE_PATHS = 8
MAX_TESTS = 16

# Task states are conceptually similar to A2A but this is NOT an A2A
# wire protocol. All transitions are local workflow metadata only.
PHASES = frozenset(
    {
        "SUBMITTED",
        "WORKING",
        "INPUT_REQUIRED",
        "AUTH_REQUIRED",
        "REVIEW_READY",
        "LOCAL_REVIEW_COMPLETE",
        "REJECTED",
        "EXPIRED",
    }
)
TRANSITIONS = {
    "SUBMITTED": frozenset({"WORKING", "REJECTED", "EXPIRED"}),
    "WORKING": frozenset({"INPUT_REQUIRED", "AUTH_REQUIRED", "REVIEW_READY", "REJECTED", "EXPIRED"}),
    "INPUT_REQUIRED": frozenset({"WORKING", "REJECTED", "EXPIRED"}),
    "AUTH_REQUIRED": frozenset({"WORKING", "REJECTED", "EXPIRED"}),
    "REVIEW_READY": frozenset({"LOCAL_REVIEW_COMPLETE", "REJECTED", "EXPIRED"}),
    "LOCAL_REVIEW_COMPLETE": frozenset(),
    "REJECTED": frozenset(),
    "EXPIRED": frozenset(),
}


def transition(previous: Any, next_phase: Any) -> str:
    """Local workflow-only state transition. Never authenticates a user."""
    need(
        type(previous) is str and previous in PHASES and type(next_phase) is str and next_phase in PHASES,
        "TASK_PHASE_INVALID",
    )
    need(next_phase in TRANSITIONS[previous], "TASK_TRANSITION_DENIED")
    return next_phase


FORBIDDEN_PARTS = {
    ".git",
    ".studio",
    ".env",
    ".github",
    "private",
    "secrets",
    "credentials",
    "keys",
    "key",
    "checkpoints",
    "tokens",
}


class PactHold(ValueError):
    """Intent or evidence does not satisfy the exact source-review contract."""


def need(condition: bool, code: str) -> None:
    if not condition:
        raise PactHold(code)


def canonical(item: Any) -> bytes:
    try:
        raw = json.dumps(
            item,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise PactHold("CANONICAL_ENCODING_UNAVAILABLE") from None
    need(len(raw) <= MAX_BYTES, "ARTIFACT_TOO_LARGE")
    return raw


def digest(item: Any) -> str:
    return hashlib.sha256(canonical(item)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "DUPLICATE_JSON_FIELD")
        result[key] = value
    return result


def _nonfinite(_):
    raise PactHold("NONFINITE_JSON")


def read_json(path: Path) -> Any:
    need(path.is_file() and not path.is_symlink(), "INPUT_NOT_REGULAR_FILE")
    need(0 < path.stat().st_size <= MAX_BYTES, "INPUT_BYTE_BUDGET")
    try:
        return json.loads(
            path.read_bytes().decode("utf-8", "strict"),
            object_pairs_hook=_pairs,
            parse_constant=_nonfinite,
        )
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        raise PactHold("INPUT_JSON_INVALID") from None


def fields(item: Any, names: set[str], code: str) -> None:
    need(type(item) is dict and set(item) == names, code)


def _sha(value: Any, *, size=40) -> bool:
    return type(value) is str and (SHA40 if size == 40 else SHA64).fullmatch(value) is not None


def path_allowed(name: Any) -> bool:
    if type(name) is not str or not 3 <= len(name) <= 180 or "\\" in name:
        return False
    p = PurePosixPath(name)
    if p.is_absolute() or len(p.parts) < 2 or p.as_posix() != name:
        return False
    if any(part in (".", "..") or part.lower() in FORBIDDEN_PARTS or part.startswith(".") for part in p.parts):
        return False
    return p.suffix.lower() in {".py", ".md", ".js", ".json", ".toml"}


def time_utc(value: Any) -> datetime:
    need(type(value) is str and STAMP.fullmatch(value) is not None, "UTC_TIMESTAMP_INVALID")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise PactHold("UTC_TIMESTAMP_INVALID") from None


def validate_task(task: Any, now: datetime) -> None:
    fields(
        task,
        {
            "schema",
            "task_id",
            "source_commit",
            "source_tree",
            "created_utc",
            "expires_utc",
            "allowed_paths",
            "goal",
            "max_changed_files",
            "visibility",
        },
        "TASK_FIELDS",
    )
    need(task["schema"] == TASK, "TASK_VERSION")
    need(type(task["task_id"]) is str and TASK_ID.fullmatch(task["task_id"]) is not None, "TASK_ID_INVALID")
    need(_sha(task["source_commit"]) and _sha(task["source_tree"]), "SOURCE_PIN_INVALID")
    paths = task["allowed_paths"]
    need(
        type(paths) is list
        and 1 <= len(paths) <= MAX_FILE_PATHS
        and all(path_allowed(x) for x in paths)
        and len(paths) == len(set(paths)),
        "TASK_PATH_POLICY",
    )
    need(type(task["goal"]) is str and 16 <= len(task["goal"]) <= 1600 and "\x00" not in task["goal"], "TASK_GOAL")
    need(type(task["max_changed_files"]) is int and 1 <= task["max_changed_files"] <= len(paths), "TASK_CHANGE_BUDGET")
    need(task["visibility"] == "PUBLIC_SOURCE_ONLY", "PRIVATE_SOURCE_NOT_ALLOWED")
    created, expires = time_utc(task["created_utc"]), time_utc(task["expires_utc"])
    need(timedelta(seconds=0) < expires - created <= timedelta(hours=24), "TASK_EXPIRY_WINDOW")
    need(created <= now < expires, "TASK_EXPIRED_OR_FUTURE")


def validate_proposal(proposal: Any, task: dict) -> None:
    fields(
        proposal,
        {
            "schema",
            "task_sha256",
            "proposer",
            "source_commit",
            "source_tree",
            "candidate_sha256",
            "changed_paths",
            "summary",
            "production_authorized",
        },
        "PROPOSAL_FIELDS",
    )
    need(proposal["schema"] == PROPOSAL, "PROPOSAL_VERSION")
    need(proposal["task_sha256"] == digest(task), "PROPOSAL_TASK_MISMATCH")
    need(type(proposal["proposer"]) is str and ACTOR.fullmatch(proposal["proposer"]), "PROPOSER_ID")
    need(
        proposal["source_commit"] == task["source_commit"] and proposal["source_tree"] == task["source_tree"],
        "PROPOSAL_SOURCE_DRIFT",
    )
    need(_sha(proposal["candidate_sha256"], size=64), "CANDIDATE_HASH_INVALID")
    paths = proposal["changed_paths"]
    need(
        type(paths) is list
        and 1 <= len(paths) <= task["max_changed_files"]
        and all(type(x) is str and x in task["allowed_paths"] for x in paths)
        and len(set(paths)) == len(paths),
        "PROPOSAL_CHANGED_PATHS",
    )
    need(type(proposal["summary"]) is str and 12 <= len(proposal["summary"]) <= 2000, "PROPOSAL_SUMMARY")
    need(proposal["production_authorized"] is False, "PROPOSAL_FALSE_PRODUCTION_AUTHORITY")


def validate_review(review: Any, task: dict, proposal: dict) -> None:
    fields(
        review,
        {
            "schema",
            "task_sha256",
            "proposal_sha256",
            "reviewer",
            "verdict",
            "findings",
            "adversarial_test_ids",
            "production_authorized",
        },
        "REVIEW_FIELDS",
    )
    need(review["schema"] == REVIEW, "REVIEW_VERSION")
    need(
        review["task_sha256"] == digest(task) and review["proposal_sha256"] == digest(proposal),
        "REVIEW_NOT_BOUND_TO_PROPOSAL",
    )
    need(type(review["reviewer"]) is str and ACTOR.fullmatch(review["reviewer"]), "REVIEWER_ID")
    need(review["reviewer"] != proposal["proposer"], "SELF_REVIEW_NOT_INDEPENDENT")
    need(review["verdict"] in ("APPROVE", "VETO", "REQUEST_CHANGES"), "REVIEW_VERDICT")
    findings = review["findings"]
    need(type(findings) is list and len(findings) <= MAX_TESTS, "REVIEW_FINDING_BUDGET")
    found_ids = set()
    for f in findings:
        fields(f, {"id", "status", "test_id"}, "FINDING_FIELDS")
        need(
            type(f["id"]) is str and TEST_ID.fullmatch(f["id"]) and f["id"] not in found_ids,
            "DUPLICATE_OR_INVALID_FINDING",
        )
        found_ids.add(f["id"])
        need(f["status"] in ("OPEN", "CLOSED_WITH_TEST"), "FINDING_STATUS")
        need(type(f["test_id"]) is str and TEST_ID.fullmatch(f["test_id"]), "FINDING_TEST_ID")
    adversarial = review["adversarial_test_ids"]
    need(
        type(adversarial) is list
        and 1 <= len(adversarial) <= MAX_TESTS
        and all(type(t) is str and TEST_ID.fullmatch(t) for t in adversarial),
        "REVIEW_ADVERSARIAL_BUDGET",
    )
    need(len(set(adversarial)) == len(adversarial), "REVIEW_ADVERSARIAL_DUPLICATE")
    need(review["production_authorized"] is False, "REVIEW_FALSE_PRODUCTION_AUTHORITY")


def validate_tests(tests: Any, task: dict, proposal: dict, review: dict) -> None:
    fields(
        tests,
        {
            "schema",
            "task_sha256",
            "candidate_sha256",
            "source_commit",
            "source_tree",
            "harness",
            "results",
            "receipt_signed",
            "production_authorized",
        },
        "TEST_REPORT_FIELDS",
    )
    need(tests["schema"] == TESTS, "TEST_REPORT_VERSION")
    need(
        tests["task_sha256"] == digest(task) and tests["candidate_sha256"] == proposal["candidate_sha256"],
        "TEST_REPORT_CANDIDATE_DRIFT",
    )
    need(
        tests["source_commit"] == task["source_commit"] and tests["source_tree"] == task["source_tree"],
        "TEST_REPORT_SOURCE_DRIFT",
    )
    need(tests["harness"] == "LOCAL_OFFLINE_UNATTESTED", "TEST_HARNESS_UNKNOWN")
    need(tests["receipt_signed"] is False and tests["production_authorized"] is False, "TEST_REPORT_FALSE_AUTHORITY")
    results = tests["results"]
    need(type(results) is list and 1 <= len(results) <= MAX_TESTS, "TEST_COUNT_BUDGET")
    observed = {}
    for r in results:
        fields(r, {"test_id", "kind", "baseline", "candidate", "case_sha256"}, "TEST_RESULT_FIELDS")
        tid = r["test_id"]
        need(type(tid) is str and TEST_ID.fullmatch(tid) and tid not in observed, "DUPLICATE_OR_INVALID_TEST_ID")
        observed[tid] = r
        need(r["kind"] in ("REGRESSION", "NEGATIVE_CONTROL"), "TEST_KIND")
        need(r["baseline"] in ("PASS", "FAIL", "NOT_RUN"), "TEST_BASELINE")
        need(r["candidate"] in ("PASS", "FAIL", "NOT_RUN"), "TEST_CANDIDATE")
        need(_sha(r["case_sha256"], size=64), "TEST_CASE_HASH")
    # The checker cannot verify the test runner. These are declared results.
    for tid in review["adversarial_test_ids"]:
        need(tid in observed, "REVIEW_NEGATIVE_TEST_MISSING")
        result = observed[tid]
        need(
            result["kind"] == "NEGATIVE_CONTROL" and result["baseline"] == "FAIL" and result["candidate"] == "PASS",
            "COUNTERFACTUAL_NEGATIVE_CONTROL_NOT_DEMONSTRATED",
        )
    for finding in review["findings"]:
        need(finding["test_id"] in observed, "FINDING_TEST_MISSING")
        if finding["status"] == "CLOSED_WITH_TEST":
            need(observed[finding["test_id"]]["candidate"] == "PASS", "CLOSED_FINDING_TEST_NOT_PASSING")
    need(all(r["candidate"] == "PASS" for r in results), "CANDIDATE_TEST_FAILURE")


def evaluate(task, proposal, review, tests, *, now=None, source_binding=None) -> dict:
    now = now or datetime.now(UTC)
    need(type(now) is datetime and now.tzinfo is not None, "CLOCK_NOT_TIMEZONE_AWARE")
    validate_task(task, now.astimezone(UTC))
    validate_proposal(proposal, task)
    validate_review(review, task, proposal)
    validate_tests(tests, task, proposal, review)
    need(
        type(source_binding) is dict and set(source_binding) == {"commit", "tree", "tracked_clean"},
        "SOURCE_READBACK_REQUIRED",
    )
    need(
        source_binding["commit"] == task["source_commit"]
        and source_binding["tree"] == task["source_tree"]
        and source_binding["tracked_clean"] is True,
        "SOURCE_CHECKOUT_DRIFT",
    )
    need(review["verdict"] == "APPROVE", "REVIEW_DISSENT_OR_VETO")
    need(not any(f["status"] == "OPEN" for f in review["findings"]), "UNRESOLVED_DISSENT_DEBT")
    return {
        "state": "REVIEW_READY_UNATTESTED",
        "reason_codes": [],
        "task_sha256": digest(task),
        "proposal_sha256": digest(proposal),
        "review_sha256": digest(review),
        "declared_tests_sha256": digest(tests),
        "declared_actor_separation": True,
        "negative_control_covered": True,
        "unresolved_dissent": 0,
        "source_checkout_locally_checked": True,
        "test_runner_independently_authenticated": False,
        "actors_cryptographically_authenticated": False,
        "receipt_signed": False,
        "model_quality_verified": False,
        "merge_authorized": False,
        "deployment_authorized": False,
    }


def git_binding(repo: Path, task: dict) -> dict:
    need(repo.is_dir() and not repo.is_symlink(), "REPOSITORY_NOT_DIRECTORY")

    def git(*args):
        try:
            return (
                subprocess.check_output(
                    ["git", "-C", str(repo), *args],
                    stderr=subprocess.DEVNULL,
                    timeout=12,
                )
                .decode("ascii", "strict")
                .strip()
            )
        except (OSError, UnicodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise PactHold("GIT_READBACK_UNAVAILABLE") from None

    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    status = git("status", "--porcelain=v1", "--untracked-files=no")
    need(not status, "TRACKED_SOURCE_DIRTY")
    need(head == task["source_commit"] and tree == task["source_tree"], "SOURCE_COMMIT_OR_TREE_CHANGED")
    return {"commit": head, "tree": tree, "tracked_clean": True}


def write_round(folder: Path, report: dict, *, round_number: int) -> dict:
    fields(
        report,
        {
            "state",
            "reason_codes",
            "task_sha256",
            "proposal_sha256",
            "review_sha256",
            "declared_tests_sha256",
            "declared_actor_separation",
            "negative_control_covered",
            "unresolved_dissent",
            "source_checkout_locally_checked",
            "test_runner_independently_authenticated",
            "actors_cryptographically_authenticated",
            "receipt_signed",
            "model_quality_verified",
            "merge_authorized",
            "deployment_authorized",
        },
        "RECEIPT_FIELDS",
    )
    need(
        report["reason_codes"] == []
        and report["declared_actor_separation"] is True
        and report["negative_control_covered"] is True
        and type(report["unresolved_dissent"]) is int
        and report["unresolved_dissent"] == 0
        and report["source_checkout_locally_checked"] is True
        and report["model_quality_verified"] is False,
        "RECEIPT_FALSE_REVIEW_EVIDENCE",
    )
    need(
        type(report) is dict
        and report.get("state") == "REVIEW_READY_UNATTESTED"
        and report.get("merge_authorized") is False
        and report.get("deployment_authorized") is False
        and report.get("receipt_signed") is False
        and report.get("actors_cryptographically_authenticated") is False
        and report.get("test_runner_independently_authenticated") is False,
        "RECEIPT_FALSE_AUTHORITY",
    )
    need(
        all(
            _sha(report.get(name), size=64)
            for name in ("task_sha256", "proposal_sha256", "review_sha256", "declared_tests_sha256")
        ),
        "RECEIPT_SOURCE_HASH_INVALID",
    )
    need(type(round_number) is int and 1 <= round_number <= 3, "ROUND_BUDGET")
    if round_number == 1:
        need(not folder.exists(), "RECEIPT_FOLDER_ALREADY_EXISTS")
        folder.mkdir(parents=True, exist_ok=False)
        prior = None
    else:
        need(folder.is_dir() and not folder.is_symlink(), "RECEIPT_FOLDER_MISSING")
        previous = folder / f"round-{round_number - 1:02d}.json"
        previous_hash = folder / f"round-{round_number - 1:02d}.sha256"
        need(
            previous.is_file()
            and previous_hash.is_file()
            and not previous.is_symlink()
            and not previous_hash.is_symlink(),
            "PRIOR_RECEIPT_MISSING",
        )
        prior = hashlib.sha256(previous.read_bytes()).hexdigest()
        need(
            previous_hash.read_text("ascii") == f"{prior}  round-{round_number - 1:02d}.json\n",
            "PRIOR_RECEIPT_HASH_MISMATCH",
        )
        try:
            last = json.loads(previous.read_bytes())
        except (UnicodeError, ValueError):
            raise PactHold("PRIOR_RECEIPT_INVALID") from None
        need(
            type(last) is dict
            and last.get("schema") == RECEIPT
            and type(last.get("result")) is dict
            and all(
                last["result"].get(k) == report[k]
                for k in ("task_sha256", "proposal_sha256", "review_sha256", "declared_tests_sha256")
            ),
            "CROSS_TASK_OR_PROPOSAL_CHAIN_REFUSED",
        )
    rec = {
        "schema": RECEIPT,
        "round": round_number,
        "previous_receipt_sha256": prior,
        "observed_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "result": report,
    }
    raw = canonical(rec) + b"\n"
    name = f"round-{round_number:02d}"
    with (folder / f"{name}.json").open("xb") as out:
        out.write(raw)
    sha = hashlib.sha256(raw).hexdigest()
    with (folder / f"{name}.sha256").open("xb") as out:
        out.write(f"{sha}  {name}.json\n".encode("ascii"))
    return {"round": round_number, "receipt_sha256": sha, "state": report["state"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded offline source-reviewed proposer/skeptic pact; never executes patches."
    )
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--source-checkout", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--round", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        task = read_json(args.task)
        proposal = read_json(args.proposal)
        review = read_json(args.review)
        tests = read_json(args.tests)
        source = git_binding(args.source_checkout, task)
        result = evaluate(task, proposal, review, tests, source_binding=source)
        outcome = write_round(args.outdir, result, round_number=args.round)
        print(json.dumps(outcome, sort_keys=True))
        return 0  # Only a source-review candidate, NEVER deployment admission.
    except (PactHold, OSError, FileExistsError) as exc:
        code = str(exc) if isinstance(exc, PactHold) else type(exc).__name__
        print(
            json.dumps(
                {"state": "HOLD", "reason": code, "merge_authorized": False, "deployment_authorized": False},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
