import http.server
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import store_deploy_readback as readback_module
from store_deploy_readback import classify, fetch, main, readback

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "f" * 40
URL = "https://agents-store.example/healthz"


def health(revision=SHA, **changes):
    body = {"evidence_class": "DECLARED", "source_revision": revision, "database_bound": True,
            "authentication_configured": True, "admission_configured": True,
            "storage_readiness": "UNKNOWN", "receipt_minted": False}
    body.update(changes)
    return 200, {"content-type": "application/json; charset=utf-8"}, json.dumps(body).encode()


class Scripted:
    """Return scripted responses in order; an exception instance is raised instead."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def run(*responses, attempts=4):
    fetcher, sleeps, lines = Scripted(*responses), [], []
    result = readback(URL, SHA, attempts=attempts, interval=7, timeout=3,
                      fetcher=fetcher, sleep=sleeps.append, log=lines.append)
    return result, fetcher, sleeps, lines


class ClassifyTests(unittest.TestCase):
    def test_exact_revision_with_bindings_is_a_match(self):
        self.assertEqual(classify(*health(), SHA)[0], "match")

    def test_abbreviated_manual_revision_is_never_a_match(self):
        # The pre-CI deploy declared SOURCE_REVISION=d739c95b; the Worker reports
        # UNKNOWN for it, and a prefix must not satisfy the readback either.
        for observed in ("UNKNOWN", SHA[:8], SHA.upper(), None, 42):
            outcome, _detail, retryable = classify(*health(observed), SHA)
            self.assertEqual(outcome, "stale")
            self.assertTrue(retryable)

    def test_paywall_and_challenge_are_reported_explicitly_and_not_retried(self):
        outcome, detail, retryable = classify(
            402, {"payment-required": "eyJ4NDAyVmVyc2lvbiI6Mn0="}, b"", SHA)
        self.assertEqual((outcome, retryable), ("edge_paywall", False))
        self.assertIn("edge returned 402 (x402 paywall)", detail)
        outcome, detail, retryable = classify(403, {"cf-mitigated": "challenge"}, b"<html>", SHA)
        self.assertEqual((outcome, retryable), ("edge_challenge", False))
        self.assertIn("403 managed challenge", detail)
        self.assertEqual(classify(403, {}, b"", SHA)[0::2], ("unexpected", True))

    def test_missing_binding_or_secret_fails_even_when_revision_matches(self):
        for field in ("database_bound", "authentication_configured", "admission_configured"):
            outcome, detail, retryable = classify(*health(**{field: False}), SHA)
            self.assertEqual((outcome, retryable), ("misconfigured", False))
            self.assertIn(field, detail)
        status, headers, body = health()
        partial = json.loads(body)
        del partial["admission_configured"]
        self.assertEqual(classify(status, headers, json.dumps(partial).encode(), SHA)[0],
                         "misconfigured")

    def test_non_json_oversized_and_non_object_bodies_are_retryable(self):
        for body in (b"<html>ok</html>", b"\xff\xfe", b"[]",
                     b" " * (readback_module.MAX_BODY_BYTES + 1)):
            outcome, _detail, retryable = classify(200, {}, body, SHA)
            self.assertEqual((outcome, retryable), ("unexpected", True))


class ReadbackTests(unittest.TestCase):
    def test_first_matching_response_succeeds_without_sleeping(self):
        result, fetcher, sleeps, lines = run(health())
        self.assertTrue(result["verified"])
        self.assertEqual((result["attempts"], sleeps), (1, []))
        self.assertEqual(fetcher.calls, [(URL, 3)])
        self.assertEqual(json.loads(lines[0])["outcome"], "match")
        self.assertEqual((result["evidence_class"], result["receipt_minted"]), ("DECLARED", False))

    def test_propagation_and_network_errors_are_retried_until_the_new_version_answers(self):
        result, _fetcher, sleeps, _lines = run(
            health("UNKNOWN"), urllib.error.URLError(TimeoutError("timed out")),
            (503, {}, b""), health())
        self.assertTrue(result["verified"])
        self.assertEqual((result["attempts"], sleeps), (4, [7, 7, 7]))

    def test_old_revision_until_the_deadline_fails(self):
        result, fetcher, sleeps, _lines = run(*[health(OTHER_SHA)] * 3, attempts=3)
        self.assertFalse(result["verified"])
        self.assertEqual(result["outcome"], "stale")
        self.assertIn(OTHER_SHA, result["detail"])
        self.assertEqual((len(fetcher.calls), sleeps), (3, [7, 7]))

    def test_edge_paywall_stops_immediately(self):
        result, fetcher, sleeps, _lines = run((402, {"payment-required": "x"}, b""), health())
        self.assertEqual((result["outcome"], result["verified"]), ("edge_paywall", False))
        self.assertEqual((len(fetcher.calls), sleeps), (1, []))

    def test_expected_revision_must_be_a_full_lowercase_sha(self):
        for value in (SHA[:8], SHA.upper(), "UNKNOWN", "", None):
            with self.assertRaises(ValueError):
                readback(URL, value, fetcher=Scripted(), sleep=lambda _s: None, log=lambda _l: None)
        with self.assertRaises(ValueError):
            readback(URL, SHA, attempts=0, fetcher=Scripted(), log=lambda _l: None)


class Handler(http.server.BaseHTTPRequestHandler):
    routes = {
        "/healthz": (200, {"content-type": "application/json"}, json.dumps({"source_revision": SHA})),
        "/paywall": (402, {"payment-required": "eyJ4NDAyVmVyc2lvbiI6Mn0="}, "Payment required"),
        "/redirect": (302, {"location": "/healthz"}, ""),
    }
    seen = []

    def do_GET(self):
        Handler.seen.append(dict(self.headers.items()))
        status, headers, body = self.routes[self.path]
        data = body.encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


class FetchOverLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        proxies = [name for name in os.environ if name.lower().endswith("_proxy")]
        self.environment = mock.patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        for name in proxies:
            del os.environ[name]
        Handler.seen = []

    def tearDown(self):
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_fetch_returns_status_headers_and_body_without_credentials(self):
        status, headers, body = fetch(self.base + "/healthz", 5)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["source_revision"], SHA)
        self.assertIn("content-type", headers)
        sent = {name.lower(): value for name, value in Handler.seen[0].items()}
        self.assertNotIn("authorization", sent)
        self.assertNotIn("cookie", sent)
        self.assertTrue(sent["user-agent"].startswith("szl-agents-studio-deploy-readback/"))

    def test_fetch_surfaces_paywall_headers_and_does_not_follow_redirects(self):
        status, headers, _body = fetch(self.base + "/paywall", 5)
        self.assertEqual((status, classify(status, headers, b"", SHA)[0]), (402, "edge_paywall"))
        status, headers, _body = fetch(self.base + "/redirect", 5)
        self.assertEqual((status, headers["location"]), (302, "/healthz"))
        self.assertEqual(len(Handler.seen), 2)


class MainTests(unittest.TestCase):
    def invoke(self, responses, *extra):
        fetcher = Scripted(*responses)
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            summary = os.path.join(directory, "summary.md")
            with mock.patch.object(readback_module, "fetch", fetcher), \
                    mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summary}), \
                    redirect_stdout(out), redirect_stderr(err):
                code = main(["--url", URL, "--expect-revision", SHA, "--attempts", "1", *extra])
            with open(summary, encoding="utf-8") as handle:
                text = handle.read()
        return code, out.getvalue(), err.getvalue(), text

    def test_match_exits_zero_and_records_a_summary(self):
        code, out, err, summary = self.invoke([health()])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.strip().splitlines()[-1])["verified"])
        self.assertEqual(err, "")
        self.assertIn("**PASS** `match`", summary)
        self.assertIn(SHA, summary)

    def test_paywall_exits_nonzero_with_an_explicit_annotation(self):
        code, _out, err, summary = self.invoke([(402, {}, b"")])
        self.assertEqual(code, 1)
        self.assertIn("::error title=Store deploy readback::edge_paywall", err)
        self.assertIn("edge returned 402 (x402 paywall)", err)
        self.assertIn("**FAIL**", summary)

    def test_cli_rejects_plain_http_and_short_revisions(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["--url", "http://agents-store.example/healthz", "--expect-revision", SHA])
            self.assertEqual(raised.exception.code, 2)
            with self.assertRaises(SystemExit) as raised:
                main(["--url", URL, "--expect-revision", "d739c95b"])
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
