#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$HOME/.mcporter" "$HOME/.config/agent-messenger" "${TODOMATE_STATE_DIR:-/tmp/todomate-slack-daily-reporter/state}" "${TODOMATE_LOG_DIR:-/tmp/todomate-slack-daily-reporter/logs}"

cat > "$HOME/.mcporter/mcporter.json" <<'JSON'
{"mcpServers":{"mcp-gateway":{"baseUrl":"https://playmcp.kakao.com/mcp","auth":"oauth"}}}
JSON

if [[ -n "${MCPORTER_CREDENTIALS_JSON_B64:-}" ]]; then
  printf '%s' "$MCPORTER_CREDENTIALS_JSON_B64" | base64 -d > "$HOME/.mcporter/credentials.json"
  chmod 600 "$HOME/.mcporter/credentials.json"
fi

if [[ -n "${AGENT_MESSENGER_SLACK_CREDENTIALS_JSON_B64:-}" ]]; then
  printf '%s' "$AGENT_MESSENGER_SLACK_CREDENTIALS_JSON_B64" | base64 -d > "$HOME/.config/agent-messenger/slack-credentials.json"
  chmod 600 "$HOME/.config/agent-messenger/slack-credentials.json"
fi

export TODOMATE_MCPORTER_PATHS="${TODOMATE_MCPORTER_PATHS:-mcporter,/app/node_modules/.bin/mcporter}"
export TODOMATE_AGENT_SLACK_PATHS="${TODOMATE_AGENT_SLACK_PATHS:-agent-slack,/app/node_modules/.bin/agent-slack}"
export TODOMATE_SLACK_SEND_METHOD="${TODOMATE_SLACK_SEND_METHOD:-agent-slack}"
export TODOMATE_TIMEZONE="${TODOMATE_TIMEZONE:-Asia/Seoul}"
export TODOMATE_STATE_DIR="${TODOMATE_STATE_DIR:-/tmp/todomate-slack-daily-reporter/state}"
export TODOMATE_LOG_DIR="${TODOMATE_LOG_DIR:-/tmp/todomate-slack-daily-reporter/logs}"

mode="${TODOMATE_RUN_MODE:-auto}"
if [[ "$mode" == "auto" ]]; then
  current_hour="$(TZ="${TODOMATE_TIMEZONE:-Asia/Seoul}" date +%H)"
  current_minute="$(TZ="${TODOMATE_TIMEZONE:-Asia/Seoul}" date +%M)"
  current_minutes=$((10#$current_hour * 60 + 10#$current_minute))
  tolerance="${TODOMATE_SCHEDULE_TOLERANCE_MINUTES:-10}"
  morning_minutes=$((9 * 60 + 1))
  evening_minutes=$((18 * 60 + 1))
  if (( current_minutes >= morning_minutes - tolerance && current_minutes <= morning_minutes + tolerance )); then
    mode="morning"
  elif (( current_minutes >= evening_minutes - tolerance && current_minutes <= evening_minutes + tolerance )); then
    mode="evening"
  else
    echo "skip: not scheduled window for auto in ${TODOMATE_TIMEZONE:-Asia/Seoul}"
    exit 0
  fi
fi
case "$mode" in
  diagnose|morning|evening) ;;
  *) echo "Invalid TODOMATE_RUN_MODE=$mode" >&2; exit 64 ;;
esac


if [[ "${TODOMATE_HERMES_HEALTHCHECK:-1}" == "1" || "${TODOMATE_HERMES_HEALTHCHECK:-true}" == "true" ]]; then
  python3 /app/hermes_playmcp_healthcheck.py || true
fi

args=("$mode")
if [[ "${TODOMATE_FORCE:-0}" == "1" || "${TODOMATE_FORCE:-false}" == "true" ]]; then
  args+=("--force")
fi

exec python3 /app/send-todomate-slack-report.py "${args[@]}"
