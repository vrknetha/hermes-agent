# Gateway Platform Adapter Best Practices

## Message Gating
- Keep platform-specific mention/reply gating inside the adapter, not in `gateway/run.py`.
- For Telegram group chats, treat `require_mention=true` as strict mode:
  - accept only when message targets Hermes (mention, reply-to-bot, or trigger prefix)
  - keep DMs unaffected
- Apply gating at the dispatch point that reflects final message shape:
  - text after batching flush
  - album/media batches at batch flush
  - immediate-only message types before `handle_message`

## Config Contract
- Bridge top-level platform config into `platforms.<name>.extra` in `gateway/config.py`.
- Preserve precedence: explicit `platforms.<name>.extra` values override bridged top-level defaults.
- Normalize config inputs (bool-like values, list/csv prefixes) before adapters consume them.

## Adapter Maintainability
- Keep parsing helpers small and composable (detect mention, detect reply, detect prefix, strip mention).
- Avoid cross-tool assumptions in adapter logic; adapters should only normalize and route messages.
- Cache platform identity metadata (bot id/username) once on connect and clear it on disconnect.

## Tests
- Add regression tests for both pass and block paths.
- Cover at least one batched path and one non-text path in platform gating tests.
- Keep tests isolated from real user home paths and external network dependencies.
