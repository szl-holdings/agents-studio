---
title: SZL Agents Studio
emoji: "\U0001F4AC"
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
hf_oauth: true
hf_oauth_expiration_minutes: 480
license: apache-2.0
---

# SZL Agents Studio

Source: private `szl-holdings/agents-studio`, `hosted/`.
Public account access uses Hugging Face OpenID Connect (`openid profile` only).
Conversations are isolated by issuer and subject, with Cloudflare D1 persistence.

Text generation uses the unmodified Qwen/Qwen2.5-0.5B-Instruct open model on CPU.
The model proposes text only. It cannot execute tools, browse, or change external
systems. Model quality is UNKNOWN until a separately recorded evaluation.
Energy measurement is UNAVAILABLE. No paid inference provider is called.

CPU Basic is best effort, may sleep, and does not provide an always-on SLA.
The 45-second generation limit is best effort, not a hard execution deadline.
Source, deployment, login, persistence and generation evidence are separate.

Prior art: [Qwen2.5 model](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct),
[HF OAuth](https://huggingface.co/docs/hub/en/spaces-oauth),
[Authlib](https://docs.authlib.org/en/latest/client/flask.html),
[Cloudflare D1](https://developers.cloudflare.com/d1/).
