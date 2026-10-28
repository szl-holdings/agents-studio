"""Agents Studio end-to-end witness: sign-in, readiness, generation, persistence, erasure.

Run by the owner on their own PC. It opens a separate Edge window with its own
profile; the owner signs in to Hugging Face and approves the app there. Every
other step runs automatically. Erasure runs only after the owner types ERASE.

Prints and saves statuses only. Never prints or saves tokens, cookies, CSRF
values or authorization codes.
"""
import datetime
import json
import os
import pathlib
import sys
import time
import uuid

from playwright.sync_api import sync_playwright

BASE = "https://szlholdings-agents-studio.hf.space"
SPACE_PAGE = "https://huggingface.co/spaces/SZLHOLDINGS/agents-studio"
LOGIN = "https://huggingface.co/login?next=%2Fspaces%2FSZLHOLDINGS%2Fagents-studio"
PROMPT = "Reply with exactly the word READY."
HERE = pathlib.Path(__file__).resolve().parent
PROFILE = pathlib.Path(os.environ["LOCALAPPDATA"]) / "szl-agents-studio-witness" / "profile"
SIGN_IN_SECONDS = 600

# Same-origin call made from the app page, matching studio.js api().
CALL_JS = """async ([path, method, body, csrf]) => {
  const options = {method, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf || ''}};
  if (body !== null) options.body = body;
  const response = await fetch(path, options);
  let parsed = null;
  try { parsed = await response.json(); } catch (error) {}
  return {status: response.status, body: parsed};
}"""

checks = []


def record(name, ok, detail):
    checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return ok


def call(page, path, method="GET", body=None, csrf=""):
    payload = None if body is None else json.dumps(body)
    return page.evaluate(CALL_JS, [path, method, payload, csrf])


def scrub(value):
    # Owner keys are pseudonymous, but the receipt does not need them.
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in {"owner", "owner_tag", "csrf"}}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def app_session(ctx):
    response = ctx.request.get(BASE + "/api/session")
    return response.json() if response.ok else {}


def wait_for(predicate, seconds, interval=3):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def open_app(ctx, page):
    page.goto(SPACE_PAGE, wait_until="domcontentloaded")
    try:
        frame = page.wait_for_selector("iframe[src*='hf.space']", timeout=60000)
        page.goto(frame.get_attribute("src"), wait_until="domcontentloaded")
    except Exception:
        page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#identity", timeout=60000)


def main():
    started = datetime.datetime.now(datetime.timezone.utc)
    witness = {"schema": "szl.agents-studio-witness/v1", "started_at": started.isoformat(),
               "base": BASE, "prompt": PROMPT}
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            # Installed Edge, with a separate witness profile (not the everyday profile).
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE), channel="msedge", headless=False,
                args=["--no-first-run", "--no-default-browser-check"])
        except Exception as error:
            print("Could not start the witness browser:", type(error).__name__, error)
            return 2
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # 1. Hugging Face sign-in (owner types their own credentials).
        if ctx.request.get("https://huggingface.co/api/whoami-v2").status != 200:
            page.goto(LOGIN, wait_until="domcontentloaded")
            print("\n>>> Sign in to Hugging Face in the Edge window that just opened.", flush=True)
            if not wait_for(lambda: ctx.request.get("https://huggingface.co/api/whoami-v2").status == 200,
                            SIGN_IN_SECONDS):
                record("huggingface_sign_in", False, "no sign-in within 10 minutes")
                ctx.close()
                return 1
        who = ctx.request.get("https://huggingface.co/api/whoami-v2").json()
        record("huggingface_sign_in", True, f"signed in as {who.get('name')}")

        # 2. Open the private Space top-level and sign in to the app (OIDC openid profile).
        open_app(ctx, page)
        if not app_session(ctx).get("user"):
            page.wait_for_selector("#login:not([disabled])", timeout=120000)
            page.click("#login")
            print(">>> If Hugging Face asks, click Authorize in the Edge window.", flush=True)
            if not wait_for(lambda: bool(app_session(ctx).get("user")), SIGN_IN_SECONDS):
                record("app_sign_in", False, "app session not established within 10 minutes")
                ctx.close()
                return 1
            page.goto(BASE + "/", wait_until="domcontentloaded")
            page.wait_for_selector("#identity", timeout=60000)
        session = call(page, "/api/session")["body"] or {}
        csrf = session.get("csrf", "")
        ready = session.get("ready", {})
        record("app_sign_in", bool(session.get("user")), f"user={session.get('user', {}).get('name')}")
        record("session_ready", all(ready.get(k) is True for k in ("auth", "store", "model")),
               f"ready={ready} store_failure={session.get('store_failure')}")

        # 3. Authenticated readiness and pinned model identity.
        readyz = call(page, "/readyz")
        witness["readyz"] = readyz
        record("readyz_authenticated", readyz["status"] == 200, f"{readyz['status']} {readyz['body']}")
        build = call(page, "/api/build-info")["body"] or {}
        model = build.get("model", {})
        witness["model"] = model
        record("model_pinned", model.get("weights_verified") is True,
               f"{model.get('model')}@{model.get('revision')} weights_verified={model.get('weights_verified')}")

        # 4. Real conversation and one bounded generation.
        created = call(page, "/api/conversations", "POST",
                       {"title": "witness " + started.strftime("%Y-%m-%dT%H:%M:%SZ")}, csrf)
        conversation_id = ((created["body"] or {}).get("conversation") or {}).get("id")
        record("conversation_create", created["status"] == 201 and bool(conversation_id),
               f"{created['status']} id={conversation_id}")
        if not conversation_id:
            return finish(ctx, witness, started)
        print("Running one CPU generation (can take up to a minute)...", flush=True)
        ran = call(page, "/api/runs", "POST",
                   {"conversation_id": conversation_id, "prompt": PROMPT,
                    "idempotency_key": str(uuid.uuid4())}, csrf)
        run = (ran["body"] or {}).get("run") or {}
        witness["run"] = scrub(ran)
        output = run.get("output") or ""
        record("generation", ran["status"] == 200 and run.get("state") == "completed" and bool(output.strip()),
               f"{ran['status']} state={run.get('state')} output={output!r}")
        record("generation_exact_ready", output.strip().rstrip(".") == "READY",
               "informational; model quality is UNKNOWN")
        record("run_model_binding",
               run.get("model_id") == model.get("model")
               and run.get("model_revision") == model.get("revision")
               and run.get("model_weights_sha256") == model.get("weights_sha256"),
               f"{run.get('model_id')}@{run.get('model_revision')} evidence={run.get('model_binding_evidence')}")

        # 5. Persistence after a full page reload.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#identity", timeout=60000)
        saved = call(page, f"/api/conversations/{conversation_id}")
        body = saved["body"] or {}
        messages = body.get("messages") or []
        runs = body.get("runs") or []
        record("persistence_readback",
               saved["status"] == 200
               and any(m.get("role") == "user" and m.get("content") == PROMPT for m in messages)
               and any(m.get("role") == "assistant" and m.get("content") == output for m in messages)
               and any(r.get("id") == run.get("id") and r.get("state") == "completed" for r in runs),
               f"{saved['status']} messages={len(messages)} runs={len(runs)}")
        listed = call(page, "/api/conversations")
        conversations = (listed["body"] or {}).get("conversations") or []
        record("conversation_listed", any(c.get("id") == conversation_id for c in conversations),
               f"{listed['status']} total_conversations={len(conversations)}")

        # 6. Erasure: owner decision only.
        print("\nThis account holds", len(conversations), "Agents Studio conversation(s):")
        for c in conversations[:20]:
            print("  -", c.get("title"))
        try:
            answer = input("\nType ERASE to permanently delete ALL of this account's Agents Studio data "
                           "(Enter skips): ").strip()
        except EOFError:
            answer = ""  # No console attached: erasure stays the owner's decision.
        if answer == "ERASE":
            csrf = (call(page, "/api/session")["body"] or {}).get("csrf", "")
            erased = call(page, "/api/account", "DELETE", {}, csrf)
            witness["erasure"] = scrub(erased)
            record("erasure", erased["status"] == 200 and (erased["body"] or {}).get("status") == "deleted",
                   f"{erased['status']} deleted={(erased['body'] or {}).get('deleted')}")
            after = call(page, "/api/session")["body"] or {}
            record("signed_out_after_erasure", after.get("user") is None, f"user={after.get('user')}")
            gone = call(page, "/api/conversations")
            record("data_inaccessible_after_erasure", gone["status"] == 401, f"{gone['status']}")
            readyz_after = call(page, "/readyz")
            record("readyz_after_erasure", readyz_after["status"] == 503
                   and (readyz_after["body"] or {}).get("store_evidence") == "UNKNOWN",
                   f"{readyz_after['status']} {readyz_after['body']}")
        else:
            witness["erasure"] = "SKIPPED_BY_OWNER"
            print("Erasure skipped. The witness conversation stays saved.")
        return finish(ctx, witness, started)


def finish(ctx, witness, started):
    ctx.close()
    witness["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    witness["checks"] = checks
    failed = [c["check"] for c in checks
              if c["result"] == "FAIL" and c["check"] != "generation_exact_ready"]
    witness["verdict"] = "PASS" if not failed else "FAIL"
    out = HERE / f"agents-studio-witness-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(witness, indent=2), encoding="utf-8")
    print(f"\nVERDICT: {witness['verdict']}" + (f" (failed: {', '.join(failed)})" if failed else ""))
    print("Receipt:", out)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
