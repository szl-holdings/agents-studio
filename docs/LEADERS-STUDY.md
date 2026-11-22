# Domain Leader Study — cohort-01 (OUROBOROS phase-3)

Date: 2026-11-22 · Method: read-only source study · Class: PROPOSE-ONLY research card.

## Domain
OpenAI Agents API curl application (offline CI contracts, no provider access in CI).

## Source references (accessed 2026-11-22)
These are primary documentation and integration guides, **not** peer-reviewed publications or evidence that this repository implements their features.

- OpenAI Agents SDK (Python) — [tracing reference](https://openai.github.io/openai-agents-python/ref/tracing/): trace/span identities and lifecycle, including generation, tool, and handoff instrumentation.
- OpenAI Agents SDK (JavaScript) — [tracing guide](https://openai.github.io/openai-agents-js/guides/tracing/): traces, spans, export lifecycle, and environment-specific limits. JavaScript semantics are reference material, not a tested Python/curl compatibility claim.
- OpenAI Agents SDK (Python) — [tracing configuration](https://openai.github.io/openai-agents-python/config/): exporter configuration and data-boundary considerations.
- Langfuse (the publisher) — [OpenAI Agents SDK integration guide](https://langfuse.com/integrations/frameworks/openai-agents): third-party instrumentation example, not an OpenAI-authored cookbook.
- Langfuse (the publisher) — [tracing/evaluation cookbook](https://langfuse.com/guides/cookbook/example_evaluating_openai_agents): illustrative offline/online evaluation patterns, not evidence of SZL execution.

## Absorb actions (proposals, **not implemented**)
1. Define a synthetic, offline curl-contract fixture distinguishing generation, tool invocation, and handoff events; do not assert byte-level compatibility with an SDK without an independent schema comparison.
2. Add a session-trace export acceptance check in offline CI using non-sensitive synthetic data, with explicit no-provider-call and no-secret-export negative tests.
3. Propose token-usage accounting only after confirming actual API fields and denominators on a pinned source fixture; do not claim live usage or spend data from this study.

## Provenance and admission
**Program receipt status: NOT AVAILABLE.** This PR does not provide an immutable OUROBOROS phase-3 program-log entry, a signed receipt, or executed comparison evidence. A future source-pinned, independently verifiable receipt is required before calling the research program-receipted.

This document makes no runtime, training, billing, API, workflow, deployment, or readiness change.
