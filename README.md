# commutecop — NYC commute orchestrator

note: vibe coded to high heck and back. this is exclusively for me to have less adhd time blindness :)

A self-hosted Python service that pulls events from Google Calendar, computes
optimal departure times factoring NYC multimodal transit, and pushes daily
digests and per-event notifications to Telegram.

## Quick start

```bash
# Install dependencies
pip install -e .

# First-time OAuth setup
commutecop oauth

# Initialize database
commutecop init-db

# Run morning digest
commutecop morning

# Run poll loop (every minute)
commutecop poll
```

## Configuration

See `examples/config.toml` and `examples/env.example` for the full configuration
schema.

## Architecture

See `plan.md` for the full architecture and implementation plan.
