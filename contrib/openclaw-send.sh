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

failures=0
sends=0

_send_one() {
  local body=$1
  if [[ -z "$body" ]]; then
    # Defensive: never POST an empty message to OpenClaw.  An empty buffer
    # usually means stdin closed mid-message; the surrounding job's stderr
    # already explains why.
    echo "openclaw-send: refusing to send empty message" >&2
    failures=$((failures + 1))
    return
  fi
  sends=$((sends + 1))
  if ! "$OPENCLAW_BIN" message send \
      --channel "$OPENCLAW_CHANNEL" \
      --target "$OPENCLAW_TARGET" \
      --message "$body"; then
    # Show a preview (truncated) so journal / cron mail surfaces the failed
    # message without dumping multi-KB bodies.
    local preview=${body:0:80}
    echo "openclaw-send: send failed for message ${preview//$'\n'/ }…" >&2
    failures=$((failures + 1))
  fi
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

# If we were mid-message when stdin ended (no terminating END marker), the
# message is incomplete — log and treat as a failure so the operator notices.
if [[ "$in_msg" == "1" ]]; then
  echo "openclaw-send: stdin ended mid-message (no END marker) — discarding buffer" >&2
  failures=$((failures + 1))
fi

echo "openclaw-send: $sends sent, $failures failed" >&2

# Exit non-zero if any send failed so cron / systemd surfaces the problem.
if (( failures > 0 )); then
  exit 1
fi
