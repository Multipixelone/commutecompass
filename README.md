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
- [`tomorrow`](./src/commutecompass/jobs/) — push tomorrow's earliest prep time to the configured HA script (pull-model alarm)
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

Point your OpenClaw instance at `skills/commutecompass/`, and the chat commands are live. The scripts shell out to `commutecompass-skill`, a wrapper the NixOS module installs when `services.commutecompass.skill.users` is set; it sources the secrets env file and points at `/etc/commutecompass/config.toml`, so the calling session needs no preamble. Outside NixOS, ship an equivalent wrapper on PATH. The legacy direct-Telegram path is still available via `[notify].mode = "telegram"` if you'd rather not depend on OpenClaw.

## Home Assistant alarm

CommuteCompass already pulls live location from Home Assistant when `[home_assistant].enabled = true`. With the optional `[home_assistant.alarm]` block it will *additionally* POST to an HA service every time a `prep` or `leave` ping fires — so HA can wake you up with a real alarm without changing your existing notification channel. The same `HOME_ASSISTANT_TOKEN` is reused; no new env var.

```toml
[home_assistant.alarm]
enabled = true
service = "script.commute_alarm"   # any "domain.service"
kinds   = ["prep", "leave"]        # which ping kinds trigger the alarm

# Optional pass-through merged into the HA service payload
[home_assistant.alarm.extra_data.data.push.sound]
critical = 1
name = "alarm.caf"
```

The recommended pattern is to point `service` at a small HA script you own and chain the loud parts there — iOS does not let third-party apps create real Clock-app alarms programmatically, so something like [Pushcut](https://www.pushcut.io) (its "Notification Server" sustains a custom loud tone until dismissed) plus an HA Companion critical notification fallback is the canonical "wake-from-a-nap" setup. A minimal HA script:

```yaml
script:
  commute_alarm:
    sequence:
      - service: notify.pushcut_my_iphone
        data:
          title: "{{ title }}"
          message: "{{ message }}"
          data:
            sound: "alarm-loop"   # a Pushcut alarm sound
      - service: notify.mobile_app_my_iphone   # belt-and-suspenders critical push
        data:
          title: "{{ title }}"
          message: "{{ message }}"
          data:
            push:
              sound:
                critical: 1
                name: "alarm.caf"
                volume: 1.0
```

CommuteCompass POSTs `{"title": "CommuteCompass", "message": "<ping body>", ...extra_data}` to the configured service. If the call fails, the primary notifier's send is **not** rolled back — the ping is still marked fired and won't repeat next minute.

### Pull-model tomorrow alarm

iOS won't let *any* third party create real Clock-app alarms — only Shortcuts running on-device can. The `commutecompass tomorrow` subcommand bridges that gap without keeping the phone in the loop in real time:

1. CommuteCompass plans tomorrow's events evening-of, picks the earliest `prep_at`, and POSTs it to an HA script.
2. The HA script stores the datetime in an `input_datetime` helper.
3. A daily 21:00 Shortcuts automation on your iPhone (set to "Run Immediately") reads the helper via the HA REST API and creates a Clock-app alarm at that time. No tapping required.

Enable it with:

```toml
[home_assistant.tomorrow]
enabled = true
script  = "script.commute_set_tomorrow_alarm"
```

Drop [`examples/ha/commute_tomorrow_alarm.yaml`](./examples/ha/commute_tomorrow_alarm.yaml) into your HA config (it includes both the `input_datetime` helper and the script, plus the Shortcuts recipe in comments at the bottom). Then schedule `commutecompass tomorrow` to run on an evening systemd timer (e.g. 20:45 NYC); it skips silently when there's nothing on the calendar.

## Development

```bash
nix flake check
nix build .#packages.x86_64-linux.default
```
