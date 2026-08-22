#!/usr/bin/env bash
set -euo pipefail

readonly API_URL="${GDSA_API_HEALTH_URL:-http://127.0.0.1:8765/health}"
readonly NGINX_URL="${GDSA_NGINX_HEALTH_URL:-http://127.0.0.1/health}"

curl --fail --silent --show-error --max-time 5 "$API_URL"
printf '\n'
curl --fail --silent --show-error --max-time 5 "$NGINX_URL"
printf '\n'
