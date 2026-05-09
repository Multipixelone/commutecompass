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
- [`test-notify`](./src/commutecompass/notify.py) — send a test Telegram message

## Configuration

See [`examples/config.toml`](./examples/) and [`examples/env.example`](./examples/) for the full configuration schema. Architecture and implementation notes live in [`plan.md`](./plan.md).

## Development

```bash
nix flake check
nix build .#packages.x86_64-linux.default
```
