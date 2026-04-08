# Spur — Agent Tool Reference

You have access to a webhook event relay. Use it to send notifications when things happen. These instructions work with any model or agent framework.

## Quick Reference

All endpoints accept and return JSON. Base URL is configurable (default: `http://127.0.0.1:8797`).

### Send an event
```
POST /api/events
{
  "event": "task_completed",
  "source": "my-agent",
  "title": "Database migration finished",
  "details": {
    "duration": "4m 32s",
    "tables_migrated": 12,
    "status": "success"
  }
}
```
Returns which routes matched and delivered.

### Create a route
```
POST /api/routes
{
  "name": "alerts-to-telegram",
  "source_filter": "event:task_completed",
  "destination_type": "telegram",
  "destination_config": {
    "bot_token": "123456:ABC...",
    "chat_id": "-100..."
  },
  "template": "*{event}* from {source}\n{title}\nDuration: {details.duration}"
}
```

### List routes
```
GET /api/routes
```

### Update a route
```
PATCH /api/routes/{route_id}
{"enabled": false}
```

### Delete a route
```
DELETE /api/routes/{route_id}
```

### Test a route
```
POST /api/routes/{route_id}/test
```
Sends a test event through the route to verify delivery.

### View event log
```
GET /api/events
GET /api/events?event_type=task_completed
GET /api/events?status=delivered
GET /api/events?limit=10
```

### Event statistics
```
GET /api/events/stats
```
Returns total events, delivered count, no-match count, active routes.

## Destination Types

| Type | Required Config |
|------|----------------|
| `telegram` | `bot_token`, `chat_id` |
| `slack` | `webhook_url` |
| `discord` | `webhook_url` |
| `http` | `url` |

## Source Filters

| Filter | Meaning |
|--------|---------|
| (empty) | Match all events |
| `event:card_moved` | Match specific event type |
| `source:tack` | Match specific source |
| `event:card_*` | Wildcard matching |
| `event:card_moved,source:tack` | Multiple conditions (AND) |
| `priority:critical` | Match any payload field |

## Templates

Use `{field}` or `{field.subfield}` placeholders:

```
[{event}] {title}
From: {source}
Status: {details.status}
```

Leave template empty for auto-formatted messages.

## When to Send Events

- When a task completes or fails
- When an agent encounters an error or blocker
- When approval status changes
- When a build or deployment finishes
- When monitoring detects anomalies

## When NOT to Send Events

- Routine polling or status checks
- Internal reasoning steps
- High-frequency events that would flood destinations (batch them instead)

## OpenAI Function-Calling Tool Definitions

```json
[
  {
    "type": "function",
    "function": {
      "name": "spur_send",
      "description": "Send an event to the relay. Matched routes will deliver it to configured destinations.",
      "parameters": {
        "type": "object",
        "properties": {
          "event": {"type": "string", "description": "Event type (e.g. task_completed, error, approval_needed)"},
          "source": {"type": "string", "description": "Your agent name or service name"},
          "title": {"type": "string", "description": "Short summary of what happened"},
          "details": {"type": "object", "description": "Additional structured data about the event"}
        },
        "required": ["event"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "spur_routes",
      "description": "List all configured event routes.",
      "parameters": {
        "type": "object",
        "properties": {}
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "spur_log",
      "description": "View recent events and their delivery status.",
      "parameters": {
        "type": "object",
        "properties": {
          "event_type": {"type": "string", "description": "Filter by event type"},
          "limit": {"type": "integer", "description": "Number of events to return (default 50)"}
        }
      }
    }
  }
]
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SPUR_HOST` | `127.0.0.1` | Bind address |
| `SPUR_PORT` | `8797` | Port number |
| `SPUR_DB` | `./data/spur.db` | SQLite database path |
