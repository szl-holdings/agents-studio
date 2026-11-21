"""Deterministic offline adversarial tests for the peer-coding proof pact."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import peer_pact as P

NOW = datetime(2026, 11, 19, 21, 0, tzinfo=UTC)


def fixtures():
    task = {
        "schema": P.TASK,
        "task_id": "peer-0001",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "created_utc": "2026-11-19T20:00:00Z",
        "expires_utc": "2026-11-20T02:00:00Z",
        "allowed_paths": ["tests/test_example.py", "tools/example.py"],
        "goal": "Add a fail-closed synthetic assertion and an independent review.",
        "max_changed_files": 2,
        "visibility": "PUBLIC_SOURCE_ONLY",
    }
    proposal = {
        "schema": P.PROPOSAL,
        "task_sha256": P.digest(task),
        "proposer": "kimi-cloud",
        "source_commit": task["source_commit"],
        "source_tree": task["source_tree"],
        "candidate_sha256": "d" * 64,
        "changed_paths": ["tests/test_example.py"],
        "summary": "Reject an invalid sample that the baseline implementation accepted.",
        "production_authorized": False,
    }
    review = {
        "schema": P.REVIEW,
        "task_sha256": P.digest(task),
        "proposal_sha256": P.digest(proposal),
        "reviewer": "second-agent",
        "verdict": "APPROVE",
        "findings": [{"id": "dissent-01", "status": "CLOSED_WITH_TEST", "test_id": "negative-01"}],
        "adversarial_test_ids": ["negative-01"],
        "production_authorized": False,
    }
    tests = {
        "schema": P.TESTS,
        "task_sha256": P.digest(task),
        "candidate_sha256": proposal["candidate_sha256"],
        "source_commit": task["source_commit"],
        "source_tree": task["source_tree"],
        "harness": "LOCAL_OFFLINE_UNATTESTED",
        "results": [
            {
                "test_id": "negative-01",
                "kind": "NEGATIVE_CONTROL",
                "baseline": "FAIL",
                "candidate": "PASS",
                "case_sha256": "e" * 64,
            },
            {
                "test_id": "regression-01",
                "kind": "REGRESSION",
                "baseline": "PASS",
                "candidate": "PASS",
                "case_sha256": "f" * 64,
            },
        ],
        "receipt_signed": False,
        "production_authorized": False,
    }
    binding = {"commit": task["source_commit"], "tree": task["source_tree"], "tracked_clean": True}
    return task, proposal, review, tests, binding


def decision(tup):
    task, proposal, review, tests, binding = tup
    return P.evaluate(task, proposal, review, tests, now=NOW, source_binding=binding)


class PactContractTests(unittest.TestCase):
    def setUp(self):
        self.all = list(fixtures())

    def refresh(self):
        task, proposal, review, tests, _ = self.all
        proposal["task_sha256"] = P.digest(task)
        review["task_sha256"] = P.digest(task)
        review["proposal_sha256"] = P.digest(proposal)
        tests["task_sha256"] = P.digest(task)
        tests["candidate_sha256"] = proposal["candidate_sha256"]

    def hold(self, reason):
        with self.assertRaisesRegex(P.PactHold, reason):
            decision(self.all)

    def test_good_pact_only_source_review_ready(self):
        result = decision(self.all)
        self.assertEqual(result["state"], "REVIEW_READY_UNATTESTED")
        self.assertTrue(result["negative_control_covered"])
        self.assertFalse(result["deployment_authorized"])
        self.assertFalse(result["merge_authorized"])
        self.assertFalse(result["test_runner_independently_authenticated"])
        self.assertFalse(result["actors_cryptographically_authenticated"])

    def test_self_review_is_denied(self):
        self.all[2]["reviewer"] = self.all[1]["proposer"]
        self.hold("SELF_REVIEW_NOT_INDEPENDENT")

    def test_review_cannot_reference_different_proposal(self):
        self.all[1]["candidate_sha256"] = "3" * 64
        self.hold("REVIEW_NOT_BOUND_TO_PROPOSAL")

    def test_task_revision_change_requires_refreshed_artifacts(self):
        self.all[0]["source_commit"] = "c" * 40
        self.hold("PROPOSAL_TASK_MISMATCH")

    def test_source_checkout_wrong_tree(self):
        self.all[4]["tree"] = "c" * 40
        self.hold("SOURCE_CHECKOUT_DRIFT")

    def test_no_source_verification_means_hold(self):
        self.all[4] = None
        self.hold("SOURCE_READBACK_REQUIRED")

    def test_dissent_veto_is_terminal_hold(self):
        self.all[2]["verdict"] = "VETO"
        self.hold("REVIEW_DISSENT_OR_VETO")

    def test_request_changes_is_not_approval(self):
        self.all[2]["verdict"] = "REQUEST_CHANGES"
        self.hold("REVIEW_DISSENT_OR_VETO")

    def test_unresolved_finding_blocks_approval(self):
        self.all[2]["findings"][0]["status"] = "OPEN"
        self.hold("UNRESOLVED_DISSENT_DEBT")

    def test_review_requires_adversarial_challenge(self):
        self.all[2]["adversarial_test_ids"] = []
        self.hold("REVIEW_ADVERSARIAL_BUDGET")

    def test_challenge_requires_declared_counterfactual(self):
        self.all[3]["results"][0]["baseline"] = "PASS"
        self.hold("COUNTERFACTUAL_NEGATIVE_CONTROL_NOT_DEMONSTRATED")

    def test_candidate_failure_is_rejected(self):
        self.all[3]["results"][1]["candidate"] = "FAIL"
        self.hold("CANDIDATE_TEST_FAILURE")

    def test_unrelated_negative_control_denied(self):
        self.all[2]["adversarial_test_ids"] = ["not-in-results"]
        self.hold("REVIEW_NEGATIVE_TEST_MISSING")

    def test_duplicate_test_id_denied(self):
        self.all[3]["results"][1]["test_id"] = "negative-01"
        self.hold("DUPLICATE_OR_INVALID_TEST_ID")

    def test_unapproved_file_path_denied(self):
        self.all[1]["changed_paths"] = ["secrets/key.pem"]
        self.hold("PROPOSAL_CHANGED_PATHS")

    def test_path_traversal_denied(self):
        self.all[0]["allowed_paths"] = ["tests/../secret.py"]
        self.hold("TASK_PATH_POLICY")

    def test_private_working_dir_denied(self):
        self.all[0]["allowed_paths"] = [".studio/private.py"]
        self.hold("TASK_PATH_POLICY")

    def test_windows_path_denied(self):
        self.all[0]["allowed_paths"] = ["tests\\test_bad.py"]
        self.hold("TASK_PATH_POLICY")

    def test_duplicate_allowed_paths_denied(self):
        self.all[0]["allowed_paths"] = ["tests/a.py", "tests/a.py"]
        self.hold("TASK_PATH_POLICY")

    def test_private_source_declared_is_denied(self):
        self.all[0]["visibility"] = "PRIVATE"
        self.hold("PRIVATE_SOURCE_NOT_ALLOWED")

    def test_bool_max_files_denied(self):
        self.all[0]["max_changed_files"] = True
        self.hold("TASK_CHANGE_BUDGET")

    def test_future_task_denied(self):
        self.all[0]["created_utc"] = "2026-11-19T23:00:00Z"
        self.hold("TASK_EXPIRED_OR_FUTURE")

    def test_expired_task_denied(self):
        self.all[0]["expires_utc"] = "2026-11-19T20:30:00Z"
        self.hold("TASK_EXPIRED_OR_FUTURE")

    def test_excessive_validity_window_denied(self):
        self.all[0]["expires_utc"] = "2026-11-22T01:00:00Z"
        self.hold("TASK_EXPIRY_WINDOW")

    def test_non_utc_timestamp_denied(self):
        self.all[0]["expires_utc"] = "2026-11-20T02:00:00-05:00"
        self.hold("UTC_TIMESTAMP_INVALID")

    def test_actor_must_be_exact_declared_id(self):
        self.all[2]["reviewer"] = "Second Agent"
        self.hold("REVIEWER_ID")

    def test_false_production_claim_denied(self):
        self.all[1]["production_authorized"] = True
        self.refresh()
        self.hold("PROPOSAL_FALSE_PRODUCTION_AUTHORITY")

    def test_unsigned_test_report_must_not_claim_signature(self):
        self.all[3]["receipt_signed"] = True
        self.hold("TEST_REPORT_FALSE_AUTHORITY")

    def test_unsupported_harness_denied(self):
        self.all[3]["harness"] = "MODEL_SELF_VERIFIED"
        self.hold("TEST_HARNESS_UNKNOWN")

    def test_short_candidate_hash_denied(self):
        self.all[1]["candidate_sha256"] = "d" * 8
        self.refresh()
        self.hold("CANDIDATE_HASH_INVALID")

    def test_nonfinite_json_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_bytes(b'{"ok":NaN}')
            with self.assertRaises(P.PactHold):
                P.read_json(p)

    def test_duplicate_json_keys_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_bytes(b'{"schema":1,"schema":2}')
            with self.assertRaises(P.PactHold):
                P.read_json(p)

    def test_symbolic_json_unhashable_adversarial_id_fails_closed(self):
        self.all[2]["adversarial_test_ids"] = [[]]
        with self.assertRaises(P.PactHold):
            decision(self.all)

    def test_append_only_round_one_and_two_chain(self):
        report = decision(self.all)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "new"
            r1 = P.write_round(folder, report, round_number=1)
            r2 = P.write_round(folder, report, round_number=2)
            self.assertEqual(r2["round"], 2)
            self.assertEqual(
                json.loads((folder / "round-02.json").read_bytes())["previous_receipt_sha256"], r1["receipt_sha256"]
            )
            self.assertEqual(r2["state"], "REVIEW_READY_UNATTESTED")
            with self.assertRaises(FileExistsError):
                P.write_round(folder, report, round_number=2)

    def test_previous_receipt_tamper_denied(self):
        report = decision(self.all)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "new"
            P.write_round(folder, report, round_number=1)
            (folder / "round-01.json").write_bytes(b'{"tampered":true}')
            with self.assertRaisesRegex(P.PactHold, "PRIOR_RECEIPT_HASH_MISMATCH"):
                P.write_round(folder, report, round_number=2)

    def test_chain_maximum_three_rounds(self):
        report = decision(self.all)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "new"
            for i in range(1, 4):
                P.write_round(folder, report, round_number=i)
            with self.assertRaisesRegex(P.PactHold, "ROUND_BUDGET"):
                P.write_round(folder, report, round_number=4)

    def test_forged_green_receipt_not_written(self):
        report = decision(self.all)
        report["deployment_authorized"] = True
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "new"
            with self.assertRaisesRegex(P.PactHold, "RECEIPT_FALSE_AUTHORITY"):
                P.write_round(folder, report, round_number=1)
            self.assertFalse(folder.exists())

    def test_hash_chain_cannot_splice_other_task(self):
        first = decision(self.all)
        changed = copy.deepcopy(first)
        changed["task_sha256"] = "9" * 64
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "new"
            P.write_round(folder, first, round_number=1)
            with self.assertRaisesRegex(P.PactHold, "CROSS_TASK_OR_PROPOSAL_CHAIN_REFUSED"):
                P.write_round(folder, changed, round_number=2)
            self.assertFalse((folder / "round-02.json").exists())

    def test_hash_chain_cannot_splice_other_candidate(self):
        first = decision(self.all)
        changed = copy.deepcopy(first)
        changed["proposal_sha256"] = "8" * 64
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "new"
            P.write_round(folder, first, round_number=1)
            with self.assertRaisesRegex(P.PactHold, "CROSS_TASK_OR_PROPOSAL_CHAIN_REFUSED"):
                P.write_round(folder, changed, round_number=2)

    def test_phase_state_machine_requires_ordered_workflow(self):
        self.assertEqual(P.transition("SUBMITTED", "WORKING"), "WORKING")
        self.assertEqual(P.transition("WORKING", "INPUT_REQUIRED"), "INPUT_REQUIRED")
        self.assertEqual(P.transition("INPUT_REQUIRED", "WORKING"), "WORKING")
        self.assertEqual(P.transition("WORKING", "REVIEW_READY"), "REVIEW_READY")
        self.assertEqual(P.transition("REVIEW_READY", "LOCAL_REVIEW_COMPLETE"), "LOCAL_REVIEW_COMPLETE")

    def test_phase_terminal_cannot_be_resumed(self):
        for state in ("LOCAL_REVIEW_COMPLETE", "REJECTED", "EXPIRED"):
            with self.subTest(state=state), self.assertRaisesRegex(P.PactHold, "TASK_TRANSITION_DENIED"):
                P.transition(state, "WORKING")

    def test_task_cannot_skip_peer_review(self):
        with self.assertRaisesRegex(P.PactHold, "TASK_TRANSITION_DENIED"):
            P.transition("SUBMITTED", "LOCAL_REVIEW_COMPLETE")

    def test_phase_auth_required_not_prod_auth(self):
        self.assertEqual(P.transition("WORKING", "AUTH_REQUIRED"), "AUTH_REQUIRED")
        with self.assertRaisesRegex(P.PactHold, "TASK_TRANSITION_DENIED"):
            P.transition("AUTH_REQUIRED", "LOCAL_REVIEW_COMPLETE")

    def test_unknown_phase_is_denied(self):
        with self.assertRaisesRegex(P.PactHold, "TASK_PHASE_INVALID"):
            P.transition("DEPLOYED", "WORKING")

    def test_malformed_report_wrong_test_digest_not_written(self):
        result = decision(self.all)
        result["declared_tests_sha256"] = "fake"
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "new"
            with self.assertRaisesRegex(P.PactHold, "RECEIPT_SOURCE_HASH_INVALID"):
                P.write_round(folder, result, round_number=1)

    def test_git_binding_requires_clean_pinned_source(self):
        task, *_ = fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_call(command, **kwargs):
                if "HEAD^{tree}" in command:
                    return (task["source_tree"] + "\n").encode()
                if "rev-parse" in command:
                    return (task["source_commit"] + "\n").encode()
                if "status" in command:
                    return b""
                raise AssertionError(command)

            with mock.patch.object(P.subprocess, "check_output", side_effect=fake_call):
                self.assertEqual(P.git_binding(root, task)["tree"], task["source_tree"])

    def test_git_binding_rejects_dirty(self):
        task, *_ = fixtures()
        with tempfile.TemporaryDirectory() as tmp:

            def fake_call(command, **kwargs):
                if "HEAD^{tree}" in command:
                    return (task["source_tree"] + "\n").encode()
                if "rev-parse" in command:
                    return (task["source_commit"] + "\n").encode()
                return b" M some.py\n"

            with (
                mock.patch.object(P.subprocess, "check_output", side_effect=fake_call),
                self.assertRaisesRegex(P.PactHold, "TRACKED_SOURCE_DIRTY"),
            ):
                P.git_binding(Path(tmp), task)


if __name__ == "__main__":
    unittest.main()
