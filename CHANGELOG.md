# Changelog

## [1.0.0] - 2026-04-08

Initial public release.

- Webhook event relay with 4 destination types (Telegram, Slack, Discord, HTTP)
- Route-based event matching with source filters and wildcards
- Template engine with `{field}` and `{field.subfield}` placeholders
- Auto-formatting when no template is specified
- Route CRUD API with test endpoint
- SQLite event log with filtering and statistics
- Web dashboard for route management and event monitoring
- Docker support
