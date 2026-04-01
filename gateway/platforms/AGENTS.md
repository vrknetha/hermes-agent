# AGENTS.md (gateway/platforms)

## Purpose
- Keep platform adapters predictable, provider-agnostic, and safe for open-source integrations.

## Rules
- Do not add product-specific hardcoding for one external integration in shared adapters.
- Keep outbound relay names generic in shared code and config (no product-branded keys, route names, headers, or docs examples).
- Shared webhook behavior must stay generic:
  - route handling is config-driven
  - optional session/thread controls use neutral header names
  - idempotency remains keyed by delivery identifiers
- Webhook reply routing must remain config-driven and payload-driven:
  - delivery mode selects transport
  - payload metadata selects direct vs group target
  - do not hardcode one external product contract beyond documented generic fields
- Preserve backward compatibility for existing routes unless a breaking change is explicitly approved.
- Keep security defaults fail-closed:
  - validate signatures when secrets are configured
  - avoid silent auth bypasses except explicit insecure test modes.
- Add/adjust gateway adapter tests whenever request/session/idempotency behavior changes.
