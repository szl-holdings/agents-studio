"""Read back the source revision the live store Worker declares after a deploy.

The deploy workflow runs this after `wrangler deploy --var SOURCE_REVISION:<sha>`.
It exits 0 only when the public `/healthz` endpoint declares exactly the expected
40-character commit SHA and reports its D1 binding and both required secrets as
configured. It sends no credentials and writes nothing.

Proof boundary: `/healthz` is the Worker's own DECLARED statement. A match shows
that the host serves a version carrying the deployed SOURCE_REVISION variable; it
is not an independent attestation of the running code or of storage readiness.
"""

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://agents-store.a-11-oy.com/healthz"
USER_AGENT = ("szl-agents-studio-deploy-readback/1 "
              "(+https://github.com/szl-holdings/agents-studio)")
MAX_BODY_BYTES = 65536
REQUIRED_TRUE = ("database_bound", "authentication_configured", "admission_configured")


def _is_revision(value):
    return (isinstance(value, str) and len(value) == 40
            and all(char in "0123456789abcdef" for char in value))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, timeout):
    """Return (status, lower-cased headers, body bytes); never follows redirects."""
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return (response.status, {k.lower(): v for k, v in response.headers.items()},
                    response.read(MAX_BODY_BYTES + 1))
    except urllib.error.HTTPError as error:
        try:
            headers = {k.lower(): v for k, v in (error.headers or {}).items()}
            body = error.read(MAX_BODY_BYTES + 1) if error.fp is not None else b""
        finally:
            error.close()
        return error.code, headers, body


def classify(status, headers, body, expected):
    """Classify one response as (outcome, detail, retryable).

    Outcomes: match, edge_paywall, edge_challenge, misconfigured, stale, unexpected.
    """
    if status == 402:
        flag = " with a PAYMENT-REQUIRED header" if "payment-required" in headers else ""
        return ("edge_paywall", f"edge returned 402 (x402 paywall){flag}; exempt this "
                "health path from Monetization Gateway rules", False)
    if status == 403 and headers.get("cf-mitigated", "").lower() == "challenge":
        return ("edge_challenge", "edge returned 403 managed challenge (bot protection); "
                "skip bot challenges for this health path", False)
    if status != 200:
        return ("unexpected", f"HTTP {status}", True)
    if len(body) > MAX_BODY_BYTES:
        return ("unexpected", "response body exceeds the readback bound", True)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ("unexpected", "HTTP 200 without a JSON body", True)
    if not isinstance(data, dict):
        return ("unexpected", "HTTP 200 without a JSON object", True)
    observed = data.get("source_revision")
    if observed != expected:
        shown = observed if isinstance(observed, str) and len(observed) <= 64 else repr(observed)[:64]
        return ("stale", f"live source_revision is {shown}", True)
    missing = [name for name in REQUIRED_TRUE if data.get(name) is not True]
    if missing:
        return ("misconfigured", "revision matches but the Worker reports "
                + ", ".join(f"{name}={data.get(name)!r}" for name in missing), False)
    return ("match", "live source_revision matches the deployed commit", False)


def readback(url, expected, attempts=18, interval=10.0, timeout=10.0,
             fetcher=fetch, sleep=time.sleep, log=print):
    if not _is_revision(expected):
        raise ValueError("expected revision must be 40 lowercase hexadecimal characters")
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    outcome, detail = "unexpected", "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            status, headers, body = fetcher(url, timeout)
            outcome, detail, retryable = classify(status, headers, body, expected)
        except (OSError, http.client.HTTPException) as error:
            status = None
            reason = getattr(error, "reason", error)
            name = type(reason).__name__ if isinstance(reason, BaseException) else str(reason)[:120]
            outcome, detail, retryable = "unexpected", f"network error: {name}", True
        log(json.dumps({"attempt": attempt, "http_status": status, "outcome": outcome,
                        "detail": detail}, sort_keys=True))
        if not retryable or attempt == attempts:
            break
        sleep(interval)
    return {"url": url, "expected_revision": expected, "outcome": outcome,
            "detail": detail, "attempts": attempt, "verified": outcome == "match",
            "evidence_class": "DECLARED", "receipt_minted": False}


def _summary(result):
    mark = "PASS" if result["verified"] else "FAIL"
    return ("### Store deploy readback\n\n| Check | Result |\n| --- | --- |\n"
            f"| URL | `{result['url']}` |\n"
            f"| Expected `source_revision` | `{result['expected_revision']}` |\n"
            f"| Outcome | **{mark}** `{result['outcome']}`: {result['detail']} |\n"
            f"| Attempts | {result['attempts']} |\n\n"
            "`/healthz` is the Worker's own DECLARED statement, not an independent attestation.\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--expect-revision", required=True)
    parser.add_argument("--attempts", type=int, default=18)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    if not args.url.startswith("https://"):
        parser.error("--url must be an https:// URL")
    try:
        result = readback(args.url, args.expect_revision, attempts=args.attempts,
                          interval=args.interval, timeout=args.timeout, fetcher=fetch)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, sort_keys=True))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(_summary(result))
    if not result["verified"]:
        print(f"::error title=Store deploy readback::{result['outcome']}: {result['detail']}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
