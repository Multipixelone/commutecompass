<h1 align="center">commutecompass</h1>
<div align="center">

[![Build](https://img.shields.io/github/actions/workflow/status/Multipixelone/commutecompass/ci.yml?style=for-the-badge&logo=github&label=build&color=a6e3a1&labelColor=313244&logoColor=cdd6f4)](https://github.com/Multipixelone/commutecompass/actions)
[![License](https://img.shields.io/github/license/Multipixelone/commutecompass?style=for-the-badge&logo=creativecommons&color=b4befe&labelColor=313244&logoColor=cdd6f4)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12+-fab387?style=for-the-badge&logo=python&labelColor=313244&logoColor=cdd6f4)
![Nix](https://img.shields.io/badge/nix-flakes-89b4fa?style=for-the-badge&logo=nixos&labelColor=313244&logoColor=cdd6f4)

</div>

A self-hosted [Python](https://www.python.org/) service that pulls events from Google Calendar, computes optimal departure times factoring NYC multimodal transit, and pushes daily digests and per-event notifications to Telegram.

> note: vibe coded to high heck and back. this is exclusively for me to have less adhd time blindness :)

## Commands

- [`oauth`](./src/commutecompass/cli.py) — interactive Google Calendar OAuth setup
- [`init-db`](./src/commutecompass/store.py) — initialize the SQLite schema
- [`morning`](./src/commutecompass/jobs/) — run the morning digest job
- [`poll`](./src/commutecompass/jobs/) — run the per-minute poll loop
- [`plan`](./src/commutecompass/planner.py) — replan a single event (debug)
- [`digest-preview`](./src/commutecompass/cli.py) — print today's digest from cache without sending
- [`adjust EVENT_ID --add-prep N`](./src/commutecompass/cli.py) — shift a plan's prep time by N minutes
- [`config show`](./src/commutecompass/cli.py) / [`config set KEY VALUE`](./src/commutecompass/cli.py) — view or edit allowlisted config fields
- [`test-notify`](./src/commutecompass/notify.py) — emit a test message via the configured notifier
- [`where`](./src/commutecompass/cli.py) — print the latest stored current location

## Configuration

See [`examples/config.toml`](./examples/) and [`examples/env.example`](./examples/) for the full configuration schema. Architecture and implementation notes live in [`plan.md`](./plan.md).

## OpenClaw integration

commutecompass ships an [OpenClaw](https://openclaw.ai) skill at [`skills/commutecompass/`](./skills/commutecompass/) so you can interact with it from chat — "what's on for today?", "shift my next event prep 45 min earlier", "set quiet hours to 23:00".

OpenClaw also owns the Telegram bot. Set `[notify].mode = "stdout"` in `config.toml` (the default in `examples/config.toml`) and commutecompass will emit each would-be Telegram message to stdout wrapped in delimiters. Pipe through [`contrib/openclaw-send.sh`](./contrib/openclaw-send.sh) to forward each message to `openclaw message send`:

```cron
0 6 * * *  COMMUTECOMPASS_CONFIG=/etc/commutecompass/config.toml \
           commutecompass morning | \
           OPENCLAW_TARGET=$CHAT_ID /opt/commutecompass/contrib/openclaw-send.sh

* * * * *  COMMUTECOMPASS_CONFIG=/etc/commutecompass/config.toml \
           commutecompass poll | \
           OPENCLAW_TARGET=$CHAT_ID /opt/commutecompass/contrib/openclaw-send.sh
```

Point your OpenClaw instance at `skills/commutecompass/` (set `COMMUTECOMPASS_CONFIG` in OpenClaw's env), and the chat commands are live. The legacy direct-Telegram path is still available via `[notify].mode = "telegram"` if you'd rather not depend on OpenClaw.

## Development

```bash
nix flake check
nix build .#packages.x86_64-linux.default
```
