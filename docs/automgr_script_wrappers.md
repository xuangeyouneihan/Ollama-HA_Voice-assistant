# AutoMgr Script Wrappers (for Voice Assistant exposure)

This file explains how to wrap `automgr.*` services with script entities so they can be managed in UI and exposed to voice assistant.

## Why wrappers

`automgr` integration registers services, not entities. UI entity filters only show entities.

With script wrappers, you get `script.*` entities that can be exposed in Assist.

## Important path note

The repository root `scripts/` folder is unrelated to Home Assistant scripts.

Use Home Assistant package path (example):

- `/config/packages/automgr_wrappers.yaml`

## Setup

1. Copy:

- `custom_components/automgr/script_wrappers.example.yaml`

2. Into HA config path:

- `/config/packages/automgr_wrappers.yaml`

3. Ensure `configuration.yaml` enables packages:

```yaml
homeassistant:
	packages: !include_dir_named packages/
```

4. Restart Home Assistant.

## Result behavior

Wrappers call `automgr.*` with `response_variable` and finish with `stop: response_variable`.

For update/delete confirmation, wrappers must pass the exact `confirmation_id` returned by prepare phase.

- If caller supports script response data, execution result is returned.
- If caller does not support script response data, backend still executes successfully but response may not surface in final speech.

For voice assistant reliability, keep `automgr.*` as primary tool path and use wrappers mainly for UI exposure/selection.
