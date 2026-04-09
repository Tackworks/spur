# Spur

A self-hosted webhook event relay for AI agent teams. Receives events, transforms them with templates, forwards to Telegram, Slack, Discord, or HTTP.

Route your signals, don't lose them.

## What is this?

Spur sits between your agent system and the outside world. When something happens — a task completes, an approval comes through, a build fails — Spur receives the event, matches it against your routes, formats it with a template, and delivers it to wherever your humans are: Telegram, Slack, Discord, or any HTTP endpoint.

No SDK. No message broker. No YAML config files. One Python file, SQLite event log, configure routes via API or web UI.

**Status: alpha.** Developed and tested internally on sandboxed development machines. If you deploy this: inspect the code, run in a VM or isolated environment, and back up your data before upgrading. This has not been independently security audited. See [SECURITY.md](SECURITY.md) for details.

**Auth note:** If you set `SPUR_API_KEY`, the server will require the key for all write operations via the API. However, the web UI does not currently send the API key with its requests. This means creating routes, deleting routes, and testing routes from the UI will be rejected by the server when a key is set. API clients (which include the key in headers) will work correctly. Web UI auth support is planned.

## Quick Start

```bash
pip install fastapi uvicorn
python server.py
```

Open `http://localhost:8797` in your browser. Create a route, start sending events.

### Docker

```bash
docker compose up -d
```

Or build manually:

```bash
docker build -t spur .
docker run -d -p 8797:8797 -v spur-data:/data spur
```

## For AI Agents

Give your agent the contents of [TOOL.md](TOOL.md) as context. It contains the REST API reference and tool definitions.

## How It Works

1. **Events come in** via `POST /api/events` with a JSON payload.
2. **Routes match** based on source filters (event type, source, any payload field).
3. **Templates render** the event into a human-readable message.
4. **Delivery happens** to the configured destination (Telegram, Slack, Discord, HTTP).
5. **Everything is logged** in SQLite for debugging and replay.

## Example: Tack board updates to Telegram

Create a route:
```bash
curl -X POST http://localhost:8797/api/routes \
  -H "Content-Type: application/json" \
  -d '{
    "name": "board-to-telegram",
    "source_filter": "event:card_moved",
    "destination_type": "telegram",
    "destination_config": {
      "bot_token": "123456:ABC-DEF...",
      "chat_id": "-1001234567890"
    },
    "template": "*Board Update:* {details.title}\n{details.from} -> {details.to}"
  }'
```

Point your Tack board (or any service) at `http://localhost:8797/api/events` to relay events. See [Tack](https://github.com/Tackworks/tack) for the task board.

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/events` | Receive an event (main ingress) |
| `POST` | `/api/routes` | Create a route |
| `GET` | `/api/routes` | List all routes |
| `GET` | `/api/routes/{id}` | Get a single route |
| `PATCH` | `/api/routes/{id}` | Update a route |
| `DELETE` | `/api/routes/{id}` | Delete a route |
| `POST` | `/api/routes/{id}/test` | Send a test event through a route |
| `GET` | `/api/events` | View event log (filter by `?event_type=`, `?status=`) |
| `GET` | `/api/events/stats` | Event statistics |
| `GET` | `/health` | Health check |

## Destinations

| Type | Config | Notes |
|------|--------|-------|
| `telegram` | `bot_token`, `chat_id`, `parse_mode` | Markdown by default, 4096 char limit |
| `slack` | `webhook_url` | Incoming webhook |
| `discord` | `webhook_url` | Discord webhook, 2000 char limit |
| `matrix` | `homeserver`, `room_id`, `access_token` | Matrix room via client-server API |
| `http` | `url`, `method`, `headers` | Any HTTP endpoint |

## Source Filters

Routes match events using a simple filter syntax:

| Filter | Matches |
|--------|---------|
| (empty) | All events |
| `card_moved` | Events where `event_type` equals `card_moved` |
| `event:card_moved` | Same as above, explicit field |
| `event:card_*` | Wildcard: any event starting with `card_` |
| `source:tack,event:card_moved` | Multiple conditions (AND logic) |
| `priority:critical` | Match on any payload field |

## Templates

Use `{field}` placeholders to format messages. Nested fields: `{details.title}`.

```
*{event}* from {source}
Title: {details.title}
Status: {details.status}
```

Leave the template empty for sensible auto-formatting.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SPUR_HOST` | `127.0.0.1` | Bind address |
| `SPUR_PORT` | `8797` | Port number |
| `SPUR_DB` | `./data/spur.db` | SQLite database path |
| `SPUR_API_KEY` | (none) | Optional API key for write operations (reads remain open) |

## Known Limitations

- **Web UI does not support API key auth.** If `SPUR_API_KEY` is set, the web UI cannot perform write operations (create/edit/delete routes, test routes, replay events). API clients that send the key in headers work fine. Fix planned for next release.
- **No built-in HTTPS or rate limiting.** Use a reverse proxy for production deployments. See [SECURITY.md](SECURITY.md).

## Dependencies

- Python 3.10+
- FastAPI
- Uvicorn

## License

MIT
