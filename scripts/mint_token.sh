#!/usr/bin/env bash
# Mint a Vexa per-user API token (scope=tx) for this adapter, running ON THE NAS.
# Sources ADMIN_API_TOKEN from the NAS .env, POSTs to admin-api at 127.0.0.1:8057
# (bound to localhost, so this must run on the NAS — no SSH, no Mac), parses the
# {"token": "..."} JSON, and prints the line:
#   VEXA_SUMMARIZER_TOKEN=<token>
# to stdout for YOU to append to /volume1/vexa/.env. This script does NOT write the
# .env itself (the NAS .env is hand-edited, never script-written). The token value
# is never printed in full — only its prefix + length.
#
# Why: the api-gateway's GET /meetings + /transcripts require a per-user token in the
# api_tokens table (scope "tx"), NOT the admin token. See summarizer/vexa.py.
set -euo pipefail

NAS_ENV="${NAS_ENV:-/volume1/vexa/.env}"
ADMIN_PORT="${ADMIN_API_PORT:-8057}"
USER_ID="${1:-1}"

[ -f "$NAS_ENV" ] || { echo "NAS .env not found at $NAS_ENV (set NAS_ENV)" >&2; exit 1; }

# shellcheck disable=SC1090
set -a; . "$NAS_ENV"; set +a
: "${ADMIN_API_TOKEN:?ADMIN_API_TOKEN not set in $NAS_ENV}"

# Mint the token (admin-api is bound to 127.0.0.1, so the curl runs locally on the NAS).
raw="$(curl -s -X POST -H "X-Admin-API-Key: $ADMIN_API_TOKEN" \
  "http://127.0.0.1:${ADMIN_PORT}/admin/users/${USER_ID}/tokens?scopes=tx&name=obsidian-vexa-bridge")"

tok="$(printf '%s' "$raw" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception as e:
    print("ERR: non-JSON response:", e); sys.exit(1)
tok = d.get("token") if isinstance(d, dict) else None
if not tok:
    print("ERR: no token in response:", json.dumps(d)[:300] if isinstance(d, dict) else d); sys.exit(1)
print(tok)
')"

case "$tok" in
    ERR:*) echo "$tok" >&2; echo "raw response was: $raw" >&2; exit 1 ;;
esac

# Print the env line for the user to append to /volume1/vexa/.env. Do NOT write the file.
printf 'VEXA_SUMMARIZER_TOKEN=%s\n' "$tok"
printf 'Append the line above to %s.\n' "$NAS_ENV" >&2
printf 'token: %s...%s, %d chars\n' "${tok:0:7}" "${tok: -4}" "${#tok}" >&2

# Verify against the api-gateway (HTTP code only; key not printed).
VEXA_API_URL="${VEXA_API_URL:-http://127.0.0.1:8056}"
code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 -H "X-API-Key: $tok" "${VEXA_API_URL}/meetings")"
echo "verify GET /meetings with new token -> HTTP $code" >&2
[ "$code" = "200" ] || echo "(non-200 — paste this to Claude; summarizer/vexa.py may need a tweak)" >&2
