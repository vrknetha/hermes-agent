# AGENTS.md (tools)

## Purpose
- Keep tool transport logic reusable, generic, and easy to maintain.

## Rules
- Reuse shared outbound transport helpers instead of re-implementing HTTP send logic in multiple tools.
- For outbound relay delivery, use `tools/relay_outbound.py` and keep the contract generic (`/v1/outbound`, `toAgentDid` or `groupId`).
- Enforce strict route validation in one place:
  - exactly one target for direct vs group sends
  - skip sending when message text is empty after trim
- Do not add product-branded naming in shared tool schemas, config keys, or examples.
- Add/update focused tests whenever tool routing or payload contracts change.
