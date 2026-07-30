#!/usr/bin/env bash
# Report z.ai (GLM) coding-plan quota usage and when each limit resets.
#
# Primary source is the monitoring endpoint the z.ai dashboard itself uses:
#   GET /api/monitor/usage/quota/limit   ->  percentage + nextResetTime per limit
# It is read-only, works at any usage level, and needs no chat request.
# Note: this endpoint is undocumented and may change without notice.
#
# Sibling endpoints /api/monitor/usage/{model,tool}-usage exist but return an
# empty body for API-key auth (they appear to need a browser session), so there
# is nothing further to report from them.
#
# --probe additionally sends a 1-token chat request, which is the only way to
# see the server's own "limit will reset at ..." message (429 body).

set -uo pipefail

# Load .env from the script's own directory, so the script works from any cwd.
# Already-exported environment variables win over the file.
env_file="${ZAI_ENV_FILE:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/.env}"
if [[ -f "$env_file" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    line="${line#export }"
    key="${line%%=*}"; val="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
    [[ -z "${!key:-}" ]] && export "$key=$val"
  done < "$env_file"
fi

API_BASE="${ZAI_API_BASE:-https://api.z.ai}"
QUOTA_URL="${ZAI_QUOTA_URL:-$API_BASE/api/monitor/usage/quota/limit}"
API_URL="${ZAI_API_URL:-$API_BASE/api/coding/paas/v4/chat/completions}"
API_KEY="${ZAI_API_KEY:-}"
MODEL="${ZAI_MODEL:-glm-5}"
SERVER_TZ="${ZAI_SERVER_TZ:-Asia/Shanghai}"

show_json=0
do_probe=0
for arg in "$@"; do
  case "$arg" in
    --json)  show_json=1 ;;
    --probe) do_probe=1 ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \?//'
      echo
      echo "usage: $(basename "$0") [--json] [--probe]"
      echo "  --json   print the raw quota response and exit"
      echo "  --probe  also send a 1-token chat request to confirm limited/not"
      echo
      echo "env: ZAI_API_KEY (required) ZAI_API_BASE ZAI_QUOTA_URL ZAI_API_URL"
      echo "     ZAI_MODEL ZAI_SERVER_TZ ZAI_ENV_FILE"
      echo "     read from .env next to this script; see .env.example"
      exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }

[[ -n "$API_KEY" ]] || {
  echo "ZAI_API_KEY is not set — copy .env.example to .env and add your key" >&2
  exit 2; }

quota=$(curl -sS --max-time 30 \
  -H "Authorization: $API_KEY" \
  -H "Accept-Language: en-US,en" \
  -H "Content-Type: application/json" \
  "$QUOTA_URL") || { echo "quota request failed" >&2; exit 2; }

if (( show_json )); then
  jq . <<<"$quota" 2>/dev/null || printf '%s\n' "$quota"
  exit 0
fi

jq -e '.success == true and (.data.limits | type == "array")' >/dev/null <<<"$quota" 2>/dev/null || {
  echo "unexpected quota response: $quota" >&2; exit 2; }

now=$(date +%s)

# Seconds -> "2h 12m" / "6d 04h 09m".
humanize() {
  local s=$1
  (( s <= 0 )) && { echo "now"; return; }
  local d=$((s/86400)) h=$(((s%86400)/3600)) m=$(((s%3600)/60))
  if   (( d > 0 )); then printf '%dd %02dh %02dm' "$d" "$h" "$m"
  elif (( h > 0 )); then printf '%dh %02dm'       "$h" "$m"
  else                   printf '%dm %02ds'       "$m" "$((s%60))"
  fi
}

# 20-cell meter.
bar() {
  local pct=$1 filled i out=""
  (( pct < 0 )) && pct=0; (( pct > 100 )) && pct=100
  filled=$(( (pct * 20 + 50) / 100 ))
  for ((i=0; i<20; i++)); do (( i < filled )) && out+='#' || out+='.'; done
  printf '%s' "$out"
}

# The unit/number enum is undocumented; map the combinations that are known and
# fall back to something honest rather than guessing.
label_for() {
  case "$1/$2/$3" in                      # type/unit/number
    TOKENS_LIMIT/3/5) echo "5-hour usage"  ;;
    TOKENS_LIMIT/6/1) echo "Weekly usage"  ;;
    TIME_LIMIT/5/1)   echo "Tool calls"    ;;
    *)                echo "$1 (unit=$2 number=$3)" ;;
  esac
}

level=$(jq -r '.data.level // "unknown"' <<<"$quota")
echo "Z.ai GLM coding plan — usage (plan: $level)"
echo

limited=0
while IFS=$'\t' read -r type unit number pct reset cur usage remaining details; do
  label=$(label_for "$type" "$unit" "$number")
  printf '%-14s [%s] %3s%%' "$label" "$(bar "${pct:-0}")" "${pct:-0}"

  if [[ -n "$reset" && "$reset" != "0" ]]; then
    epoch=$((reset / 1000))                       # nextResetTime is milliseconds
    printf '   resets %s  (in %s)' \
      "$(date -d "@$epoch" '+%Y-%m-%d %H:%M:%S %Z')" "$(humanize $((epoch - now)))"
  else
    printf '   resets unknown'
  fi
  echo

  [[ -n "$cur" && -n "$usage" ]] &&
    printf '%-14s %s of %s used, %s remaining\n' "" "$cur" "$usage" "${remaining:-?}"

  if [[ -n "$details" ]]; then
    IFS=',' read -ra parts <<<"$details"
    for p in "${parts[@]}"; do
      printf '%-16s %-14s %s\n' "" "${p%%=*}" "${p#*=}"
    done
  fi

  (( ${pct:-0} >= 100 )) && limited=1
done < <(jq -r '
  .data.limits[] | [
    .type, (.unit|tostring), (.number|tostring), (.percentage // 0 | tostring),
    (.nextResetTime // 0 | tostring),
    (.currentValue // "" | tostring), (.usage // "" | tostring),
    (.remaining // "" | tostring),
    ([.usageDetails[]? | "\(.modelCode)=\(.usage)"] | join(","))
  ] | @tsv' <<<"$quota")

if (( do_probe )); then
  echo
  body=$(curl -sS --max-time 30 -X POST "$API_URL" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1}") || {
      echo "probe: request failed" >&2; exit 2; }

  # A 429 body carries "Your limit will reset at <YYYY-MM-DD HH:MM:SS>" in the
  # server's timezone (UTC+8); everything else means the request went through.
  reset=$(grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' <<<"$body" | head -1)
  if [[ -n "$reset" ]]; then
    if epoch=$(TZ="$SERVER_TZ" date -d "$reset" +%s 2>/dev/null); then
      echo "probe: rate limited — server says reset at $(date -d "@$epoch" '+%Y-%m-%d %H:%M:%S %Z') (in $(humanize $((epoch - $(date +%s)))))"
    else
      echo "probe: rate limited — server says reset at $reset ($SERVER_TZ)"
    fi
    limited=1
  elif grep -q '"error"' <<<"$body"; then
    echo "probe: unexpected error: $body" >&2
    exit 2
  else
    echo "probe: chat request accepted — not currently rate limited."
  fi
fi

exit $limited
