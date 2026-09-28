# Security Policy

## Supported Versions

Security fixes are applied to the `main` branch. We recommend tracking `main`
or the latest tagged release.

## Reporting a Vulnerability

Please report security issues **privately**:

* Use GitHub **Private vulnerability reporting** (Security → Report a
  Vulnerability) on the project's repository, **or**
* Email the maintainers at `<security@example.com>` (replace with the
  maintainer's real contact before publishing).

Do **not** open a public issue for security vulnerabilities. We aim to
acknowledge reports within 5 business days and will coordinate a fix and
disclosure timeline with you.

## Data the Tool Touches

This fork adds an optional AI assistant layer (`repoai/`). Its data handling:

* **Telemetry is off by default.** `ai.telemetry.enabled` defaults to `false`.
  No usage/diagnostic events are collected or transmitted unless an operator
  explicitly configures an endpoint.
* **Offline-first.** The rule engine, offline knowledge base, and error
  diagnosis run entirely on the local machine with no network access.
* **Online enhancement is opt-in.** When `ai.gateway.url` is set, AI calls go
  **only** to the endpoint you configure (any OpenAI-compatible `/v1`
  gateway). Your gateway token (`ai.gateway.token`) is sent solely to that
  endpoint — never to the project maintainers.
* **Isolation-aware regions.** `isolated-r1`/`r2`/`r3` configurations never
  open outbound connections; data stays local or on operator-provided
  exchange directories.
* **One switch off.** Setting `ai.enabled=false` (or an admin policy lock)
  restores the stock `repo` behaviour exactly, with no AI code on the hot path.

If you deploy the optional telemetry collector (`contrib/collector-deploy.md`),
treat the collected event store as sensitive operational data and restrict
access accordingly.
