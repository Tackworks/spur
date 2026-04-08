# Changelog

## [1.1.0] - 2026-04-08

### Added
- **Dark/Light theme toggle** — Click the moon/sun icon in the header. Preference persists via localStorage.
- **Matrix destination adapter** (Issue #1) — Send events to Matrix rooms via the client-server API. Configure with homeserver URL, room ID, and access token.

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
