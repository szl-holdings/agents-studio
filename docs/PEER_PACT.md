# SZL Peer Pact v1 — proof-carrying agentic code review

**Status:** offline, source-only, unsigned, operator-initiated.
This is not an A2A server, model runner, production authorizer or patch executor.

## Pair programming with dissent debt

Kimi can propose a change to publicly releasable source. A distinct skeptic
(ChatGPT or another approved reviewer) must challenge it. A deterministic gate
checks task/source/patch/test identity, allows a finite review budget and refuses
unresolved objections. This draws on the A2A distinction between tasks and
artifacts, but **is not A2A wire-protocol compliant**.

Novel invariant: **dissent debt cannot be waived by a green vote**.

- An explicit reviewer VETO or REQUEST_CHANGES causes HOLD.
- Any OPEN finding causes HOLD even if the reviewer says APPROVE.
- A CLOSED_WITH_TEST finding must name a passing candidate test.
- At least one named NEGATIVE_CONTROL must report baseline FAIL and candidate
  PASS, on the identical source/candidate identity. This is a declaration:
  the pact does NOT independently execute or verify the test report.
- Self-review, mismatched source SHA/tree, private/unapproved files, missing
  source checkout verification, stale tasks, and false production authority HOLD.
- Multiple rounds are append-only. A second or third round must hash-link to
  the prior receipt AND use identical task/proposal/review/test digests.

Even an accepted candidate returns only REVIEW_READY_UNATTESTED. This means
local metadata is structurally suitable for a HUMAN source-review decision.
It is **not** code quality proof, independently authenticated test execution,
cryptographic authorship, permission to merge, payment or release admission.

## One-shot workflow

1. Establish publicly releasable source and exact protected Git commit/tree.
   Specify an expiring task (max 24h) and at most eight allowlisted paths.
2. Save the immutable proposal JSON (task hash, proposer ID, candidate digest
   and changed paths). No actual patch is automatically applied.
3. Have a second actor produce a review JSON bound to the proposal digest.
   Explicitly retain findings and adversarial test identifiers.
4. Produce a bounded test report (max 16). Test results in this JSON remain
   **unattested declarations** until a separate trusted harness verifies them.
5. Run the stdlib offline evaluator on a clean, exact Git checkout. It writes
   new private SHA-256 integrity receipts; prior files cannot be replaced.
6. Source owner separately conducts signed/DCO PR review and existing CI.

### Command

    python -m peer_pact --task C:\private\task.json --proposal C:\private\proposal.json --review C:\private\review.json --tests C:\private\tests.json --source-checkout C:\path\to\exact\public\checkout --outdir C:\private\fresh-pact-receipts --round 1

Exit 0: review-ready UNATTESTED. Exit 2: HOLD. Both forbid deployment.
Round 2 or 3 is a separate explicit operator invocation, with the exact same
artifact identity and matching prior receipt hash. No scheduler/daemon.

## Typed artifacts

- szl.peer-pact.task/v1: SHA40 source commit and tree, created/expiry in UTC,
  public source only, finite changed-file and task-text budgets.
- szl.peer-pact.proposal/v1: canonical task digest, proposer ID, SHA256
  candidate digest and exact changed paths.
- szl.peer-pact.challenge/v1: reviewer ID distinct from proposer, explicit
  verdict, findings and adversarial negative controls bound to the proposal.
- szl.peer-pact.tests/v1: at most 16 test IDs/results, source/candidate
  bindings and explicit lack of signature and production authority.
- szl.peer-pact.receipt/v1: task/result checksum and prior hash per round.

Local phase labels resemble A2A submitted/working/input-required/auth-required/
completed/rejected/expired but are NOT remote agent authentication, MCP,
persistent sessions, or actual signed provider task status.

## Non-authorities and follow-ups

No Ollama model calls, unrestricted shell, external provider networking,
cloud secret access, private graph hydration, training, repo merge, deployment
or automatic job replay occur in this module. A signed receipt requires a
separate actual signer and approved trust root. Two actor strings do not
prove the people/models are independent. Source visibility and rights are
declared by the operator, not independently proven by the Git check.

To move toward production: admit public-source sharing rights, independent
actor identity, trusted test replay, source-build signatures, operator leases
and branch protections. Preserve Agents Studio session locks and private
state-file boundaries. Do not wire this module into the hosted service
without a distinct security review.

Reproduce offline:

    python -B -m unittest discover -s tests -p test_peer_pact.py -v
    ruff check peer_pact.py tests/test_peer_pact.py
    ruff format --check peer_pact.py tests/test_peer_pact.py

A2A reference: https://a2a-protocol.org/latest/specification/agent-card/
