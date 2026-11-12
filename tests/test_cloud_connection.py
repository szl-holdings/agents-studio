"""Fixed-origin observations tested with local fixtures only, never cloud requests."""

from copy import deepcopy
from email.message import Message
import http.client
import io
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import cloud_connection as cloud
import studio


def documents():
    inference = {
        "status": "unavailable", "checks": dict.fromkeys(cloud.CHECKS, False),
        "remote_credentialed_provider": False, "ready_for_requests": False,
        "basis": "LOCAL_CONFIGURATION_ONLY", "provider_reachability": "UNVERIFIED",
        "inference_witness": "UNAVAILABLE", "model_identity": "UNVERIFIED",
    }
    return [
        {"schema": "szl.router-source/v1", "repository": cloud.REPOSITORY,
         "revision": cloud.EXPECTED_REVISION, "controlled_files": dict(cloud.CONTROLLED_FILES),
         "default_egress": False, "secret_output": False, "arbitrary_url_routing": False,
         "receipt": {"algorithm": "sha256", "digest": "0" * 64}},
        {"scope": "CONTROL_PLANE", "status": "ready", "registry_state": "UNAVAILABLE_NOT_CONFIGURED",
         "checks": {"static_index": True, "registry": True, "egress": True},
         "egress_enabled": False, "inference": deepcopy(inference)},
        inference,
    ]


def response(index, document=None, status=None, body=None, headers=None, url=None):
    return cloud.RemoteResponse(
        status if status is not None else (503 if index == 2 else 200),
        body if body is not None else json.dumps(documents()[index] if document is None else document).encode(),
        headers if headers is not None else {"content-type": "application/json"},
        url if url is not None else cloud.ORIGIN + cloud.PATHS[index],
    )


def observe(index=None, replacement=None):
    values = [response(i) for i in range(3)]
    if index is not None:
        values[index] = replacement
    return cloud.parse_observation(*values, "2026-10-08T00:00:00+00:00")


class CloudParserTests(unittest.TestCase):
    def test_control_ready_is_not_inference_ready(self):
        result = observe()
        self.assertEqual(result["source"]["state"], "MATCH")
        self.assertEqual(result["control"]["state"], "READY")
        self.assertEqual(result["inference"]["state"], "UNAVAILABLE")
        self.assertFalse(result["inference"]["ready_for_requests"])
        self.assertEqual(result["inference"]["provider_reachability"], "UNVERIFIED")
        self.assertEqual(result["errors"], [])
        self.assertNotIn("receipt", result)

    def test_configuration_ready_does_not_promote_runtime_witness(self):
        data = documents()[2]
        data.update(status="ready", ready_for_requests=True, remote_credentialed_provider=True)
        data["checks"] = dict.fromkeys(cloud.CHECKS, True)
        result = observe(2, response(2, data, 200))["inference"]
        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["basis"], "LOCAL_CONFIGURATION_ONLY")
        self.assertEqual(result["inference_witness"], "UNAVAILABLE")
        self.assertEqual(result["provider_reachability"], "UNVERIFIED")

    def test_revision_and_controlled_file_drift_never_source_match(self):
        for key in ("revision", "controlled_files"):
            with self.subTest(key=key):
                data = documents()[0]
                if key == "revision":
                    data[key] = "1" * 40
                else:
                    data[key]["router_control/app.py"] = "1" * 64
                result = observe(0, response(0, data))
                self.assertEqual(result["source"]["state"], "MISMATCH")
                self.assertEqual(result["errors"][0]["code"], "SOURCE_METADATA_MISMATCH")

    def test_wrong_source_identity_or_missing_field_unknown(self):
        for key, value in (("repository", "attacker/repo"), ("schema", "new/v2"),
                           ("revision", True), ("default_egress", "false"), ("controlled_files", {})):
            data = documents()[0]
            data[key] = value
            self.assertEqual(observe(0, response(0, data))["source"]["state"], "UNKNOWN")

    def test_duplicate_nonfinite_and_nested_duplicate_json_rejected(self):
        for body in (b'{"status":"ready","status":"unavailable"}', b'{"x":NaN}',
                     b'{"x":Infinity}', b'{"x":1e999}', b'{"x":{"a":1,"a":2}}',
                     b'[]', b'null', b'\xff'):
            result = observe(1, response(1, body=body))
            self.assertEqual(result["control"]["state"], "UNKNOWN")
            self.assertEqual(result["errors"][0]["code"], "INVALID_RESPONSE")

    def test_inference_types_and_contradictions_fail_closed(self):
        for key, value in (("ready_for_requests", True), ("ready_for_requests", 0),
                           ("basis", "LIVE_INFERENCE"), ("provider_reachability", "VERIFIED"),
                           ("inference_witness", "MEASURED"), ("model_identity", "VERIFIED")):
            data = documents()[2]
            data[key] = value
            result = observe(2, response(2, data))
            self.assertEqual(result["inference"]["state"], "UNKNOWN")
        for value in ("false", 0, None):
            data = documents()[2]
            data["checks"]["egress_enabled"] = value
            self.assertEqual(observe(2, response(2, data))["inference"]["state"], "UNKNOWN")

    def test_control_boolean_checks_scope_and_status_consistency(self):
        for key, value in (("scope", "INFERENCE"), ("status", "unavailable"), ("egress_enabled", 0)):
            data = documents()[1]
            data[key] = value
            self.assertEqual(observe(1, response(1, data))["control"]["state"], "UNKNOWN")
        data = documents()[1]
        data["checks"]["registry"] = "true"
        self.assertEqual(observe(1, response(1, data))["control"]["state"], "UNKNOWN")
        self.assertEqual(observe(1, response(1, status=503))["control"]["state"], "UNKNOWN")

    def test_envelope_size_type_redirect_and_identity(self):
        cases = [response(1, body=b" " * (cloud.MAX_BYTES + 1)), response(1, status=302),
                 response(1, url="https://example.invalid/readyz"),
                 response(1, headers={"content-type": "text/html"}),
                 response(1, headers={"content-type": "application/json", "content-encoding": "gzip"}),
                 response(1, headers={"content-type": "application/json", "content-length": "1"}),
                 response(1, headers={"content-type": "application/json", "content-length": "99999999"}),
                 response(1, headers={"Content-Type": "application/json", "content-type": "application/json"})]
        for value in cases:
            result = observe(1, value)
            self.assertEqual(result["control"]["state"], "UNKNOWN")
            self.assertTrue(result["errors"])

    def test_failures_use_fixed_sanitized_errors(self):
        result = observe(2, cloud.ObservationError("NETWORK_ERROR"))
        self.assertEqual(result["inference"]["state"], "UNAVAILABLE")
        self.assertEqual(result["inference"]["checks"], dict.fromkeys(cloud.CHECKS))
        self.assertEqual(observe(1, response(1, status=401))["control"]["state"], "UNAVAILABLE")


class CloudWorkerTests(unittest.TestCase):
    def test_fixed_three_reads_and_no_cached_completed_response(self):
        calls = []
        def fake(path, deadline):
            calls.append((path, deadline))
            return response(cloud.PATHS.index(path))
        observer = cloud.CloudConnection(fake)
        self.assertEqual(calls, [])
        self.assertEqual(observer.check()["control"]["state"], "READY")
        observer.check()
        self.assertEqual([path for path, _ in calls], list(cloud.PATHS) * 2)
        self.assertEqual(len({deadline for _, deadline in calls[:3]}), 1)

    def test_hard_deadline_and_no_more_workers_while_one_is_stuck(self):
        gate = threading.Event()
        calls = []
        def fake(path, deadline):
            calls.append(path)
            gate.wait(2)
            return response(0)
        observer = cloud.CloudConnection(fake, timeout=0.05)
        try:
            begin = time.monotonic()
            result = observer.check()
            self.assertLess(time.monotonic() - begin, 0.5)
            self.assertEqual(result["control"]["state"], "UNAVAILABLE")
            observer.check()
            self.assertEqual(calls, ["/api/source"])
            # Event.wait can return before the monotonic deadline. Keep the
            # stuck read held until that deadline, or the worker may still
            # open the next path.
            while time.monotonic() < observer.cycle.deadline:
                remaining = observer.cycle.deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            gate.set()
            observer.cycle.done.wait(2)
        self.assertEqual(calls, ["/api/source"])

    def test_concurrent_checks_share_inflight_only(self):
        gate, entered = threading.Event(), threading.Event()
        calls, results = [], []
        def fake(path, deadline):
            calls.append(path)
            entered.set()
            gate.wait(2)
            return response(cloud.PATHS.index(path))
        observer = cloud.CloudConnection(fake)
        one = threading.Thread(target=lambda: results.append(observer.check()))
        two = threading.Thread(target=lambda: results.append(observer.check()))
        one.start()
        self.assertTrue(entered.wait(1))
        two.start()
        time.sleep(0.03)
        gate.set()
        one.join(2)
        two.join(2)
        self.assertEqual(len(results), 2)
        self.assertEqual(calls, list(cloud.PATHS))

    def test_unexpected_exception_does_not_echo_credentials(self):
        def fake(path, deadline):
            raise OSError("Bearer sk-private-fixture-value")
        result = cloud.CloudConnection(fake).check()
        self.assertNotIn("private-fixture", json.dumps(result))
        self.assertEqual(len(result["errors"]), 3)


class CloudTransportTests(unittest.TestCase):
    def test_transport_has_verified_tls_no_proxy_auth_cookie_and_fixed_get(self):
        raw = response(1).body
        remote = mock.MagicMock()
        remote.__enter__.return_value = remote
        remote.status = 200
        remote.headers = Message()
        remote.headers["Content-Type"] = "application/json"
        remote.headers["Content-Length"] = str(len(raw))
        remote.geturl.return_value = cloud.ORIGIN + "/readyz"
        remote.read1 = io.BytesIO(raw).read1
        opener = mock.Mock()
        opener.open.return_value = remote
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "https://proxy.invalid", "OPENAI_API_KEY": "sk-fixture-secret"}), \
                mock.patch.object(cloud.urllib.request, "build_opener", return_value=opener) as build:
            result = cloud.fetch("/readyz", time.monotonic() + 1)
        self.assertEqual(result.body, raw)
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], cloud.NoRedirect)
        self.assertEqual(handlers[2]._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(handlers[2]._context.check_hostname)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, cloud.ORIGIN + "/readyz")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual({key.lower() for key in request.headers}, {"accept", "user-agent"})
        self.assertIsNone(request.data)

    def test_arbitrary_paths_and_redirects_rejected_before_follow(self):
        with mock.patch.object(cloud.urllib.request, "build_opener") as build:
            for path in ("/api/routes", "https://example.invalid", "/readyz?token=x", "//example.invalid"):
                with self.assertRaises(cloud.ObservationError):
                    cloud.fetch(path, time.monotonic() + 1)
            build.assert_not_called()
        with self.assertRaises(cloud.ObservationError) as caught:
            cloud.NoRedirect().redirect_request(None, None, 302, "moved", {}, cloud.ORIGIN + "/readyz")
        self.assertEqual(caught.exception.code, "REDIRECT")

    def test_duplicate_headers_rejected(self):
        headers = Message()
        headers["Content-Length"] = "1"
        headers["Content-Length"] = "1"
        with self.assertRaises(cloud.ObservationError):
            cloud._headers(headers)


class CloudRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="szl-cloud-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        state = self.root / "state.json"
        state.write_text(json.dumps({"project_id": app.PROJECT_ID}), encoding="utf-8")
        self.instance = studio.Studio(self.root / "data", state)
        self.check = mock.Mock(return_value=observe())
        self.instance.cloud_connection = mock.Mock(check=self.check)
        self.server = studio.make_server(self.instance, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.shutdown)

    def request(self, path="/api/cloud-status", token=True, headers=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        self.addCleanup(connection.close)
        supplied = {"X-Studio-Token": self.instance.token} if token else {}
        supplied.update(headers or {})
        connection.request(method, path, headers=supplied)
        result = connection.getresponse()
        return result.status, dict(result.getheaders()), result.read()

    def test_authenticated_get_reads_only_without_receipt_job_or_file_changes(self):
        before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.root.rglob("*") if path.is_file()}
        status, headers, raw = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["schema"], "szl.studio.cloud-connection/v1")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.check.assert_called_once_with()
        self.assertEqual(self.instance.jobs, {})
        self.assertEqual(before, {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in self.root.rglob("*") if path.is_file()})

    def test_guards_and_exact_target_precede_cloud_reads(self):
        self.assertEqual(self.request(token=False)[0], 401)
        self.assertEqual(self.request(headers={"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.request(headers={"Host": "localhost:1"})[0], 403)
        for path in ("/api/cloud-status?target=https://example.invalid", "/api/cloud-status?", "/api/cloud-status#x"):
            self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request("/api/cloud-status/extra")[0], 404)
        self.check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
