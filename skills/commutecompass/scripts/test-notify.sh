#!/usr/bin/env bash
set -euo pipefail
exec commutecompass --config "${COMMUTECOMPASS_CONFIG}" test-notify "$@"
