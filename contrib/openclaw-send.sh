#!/usr/bin/env bash
# openclaw-send.sh — bridge commutecompass stdout to `openclaw message send`.
#
# commutecompass in `notify.mode = "stdout"` wraps each would-be Telegram
# message in delimiters:
#
#     ===COMMUTECOMPASS-MSG===
#     <message text>
#     ===COMMUTECOMPASS-END===
#
# This script reads stdin, splits on those markers, and sends each block to
# OpenClaw, which delivers it to Telegram. Designed for cron/systemd:
#
#     0 6 * * *  COMMUTECOMPASS_CONFIG=/etc/commutecompass/config.toml \
#                commutecompass morning | \
#                OPENCLAW_TARGET=$CHAT_ID /opt/commutecompass/contrib/openclaw-send.sh
#
#     * * * * *  COMMUTECOMPASS_CONFIG=/etc/commutecompass/config.toml \
#                commutecompass poll | \
#                OPENCLAW_TARGET=$CHAT_ID /opt/commutecompass/contrib/openclaw-send.sh
#
# Required env:
#   OPENCLAW_TARGET   Telegram chat id, @username, or forum-topic target.
#
# Optional env:
#   OPENCLAW_BIN      Path to the openclaw binary (default: `openclaw` on PATH).
#   OPENCLAW_CHANNEL  Channel name (default: `telegram`).

set -euo pipefail

: "${OPENCLAW_TARGET:?must be set (telegram chat id / username / forum target)}"
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
OPENCLAW_CHANNEL="${OPENCLAW_CHANNEL:-telegram}"

_send_one() {
  local body=$1
  printf '%s' "$body" | "$OPENCLAW_BIN" message send \
    --channel "$OPENCLAW_CHANNEL" \
    --target "$OPENCLAW_TARGET" \
    --stdin
}

in_msg=0
buffer=""
while IFS= read -r line; do
  case "$line" in
    "===COMMUTECOMPASS-MSG===")
      in_msg=1
      buffer=""
      ;;
    "===COMMUTECOMPASS-END===")
      in_msg=0
      _send_one "$buffer"
      ;;
    *)
      if [[ "$in_msg" == "1" ]]; then
        if [[ -z "$buffer" ]]; then
          buffer="$line"
        else
          buffer="$buffer"$'\n'"$line"
        fi
      fi
      ;;
  esac
done
