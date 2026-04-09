# AutoMgr Native Services

This repository now includes a Home Assistant custom integration domain `automgr`.

## Location

- `custom_components/automgr/`

## Configuration

Add this block in Home Assistant `configuration.yaml`:

```yaml
automgr:
  server_url: http://127.0.0.1:8000
  timeout: 30
  default_language: zh
```

Then restart Home Assistant.

## Registered Services

- `automgr.automation_create`
- `automgr.automation_update`
- `automgr.automation_delete`
- `automgr.automation_confirm`
- `automgr.script_create`
- `automgr.script_update`
- `automgr.script_delete`
- `automgr.script_confirm`

## Integration scope

`automgr` now exposes services only (no native entities).

If you need entity-level exposure in UI / Assist, use script wrappers from:

- `custom_components/automgr/script_wrappers.example.yaml`

## Two-step confirmation behavior (update/delete)

1. Prepare phase:

- Call `automgr.automation_update` or `automgr.automation_delete` with:
  - `conversation_id` (optional)
  - `text`
  - `skip_confirmation: false`

2. Confirm phase:

- Call `automgr.automation_confirm` with:
  - `expected_operation`
  - `confirmation_id` (required)

or call `automgr.automation_update` / `automgr.automation_delete` with:

- `confirm: true`
- `confirmation_id` (required)

The integration stores per-confirmation context for same-turn safety checks.

The backend enforces:

- `confirmation_id` is required for all confirm actions.
- `confirmation_id` must be valid and not expired.
- pending confirmations are retained for up to 24 hours.
- confirm execution that writes/reloads YAML is serialized in FIFO order.

Same flow applies to script services (`automgr.script_*`).

## Response shape

All services return a response dictionary (supports response):

- `phase`: `prepare` or `done`
- `message`: backend message
- `confirmation_id`: present in prepare phase
- `data`: raw backend JSON body

This allows conversation agents to speak the result directly.
