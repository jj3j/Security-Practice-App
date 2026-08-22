#!/usr/bin/env bash
set -euo pipefail

readonly API_URL="${GDSA_API_HEALTH_URL:-http://127.0.0.1:8765/health}"
readonly NGINX_URL="${GDSA_NGINX_HEALTH_URL:-http://127.0.0.1/health}"
readonly HEALTH_TIMEOUT_SECONDS="${GDSA_HEALTH_TIMEOUT_SECONDS:-30}"
readonly HEALTH_RETRY_INTERVAL_SECONDS="${GDSA_HEALTH_RETRY_INTERVAL_SECONDS:-2}"

[[ "$HEALTH_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] \
    || { echo "GDSA_HEALTH_TIMEOUT_SECONDS must be a positive integer" >&2; exit 2; }
[[ "$HEALTH_RETRY_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]] \
    || { echo "GDSA_HEALTH_RETRY_INTERVAL_SECONDS must be a positive integer" >&2; exit 2; }

api_healthy=false
nginx_healthy=false
readonly deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
attempt=0

while [[ "$api_healthy" == false || "$nginx_healthy" == false ]]; do
    attempt=$((attempt + 1))

    if [[ "$api_healthy" == false ]]; then
        echo "Health-check attempt ${attempt}: API ${API_URL}"
        if curl --fail --silent --show-error --connect-timeout 2 --max-time 2 "$API_URL"; then
            printf '\n'
            api_healthy=true
            echo "API health endpoint is ready."
        fi
    fi

    if [[ "$nginx_healthy" == false ]]; then
        echo "Health-check attempt ${attempt}: Nginx ${NGINX_URL}"
        if curl --fail --silent --show-error --connect-timeout 2 --max-time 2 "$NGINX_URL"; then
            printf '\n'
            nginx_healthy=true
            echo "Nginx health endpoint is ready."
        fi
    fi

    if [[ "$api_healthy" == true && "$nginx_healthy" == true ]]; then
        echo "Deployment health checks passed."
        exit 0
    fi

    remaining=$((deadline - SECONDS))
    if (( remaining <= 0 )); then
        break
    fi
    sleep_for="$HEALTH_RETRY_INTERVAL_SECONDS"
    if (( sleep_for > remaining )); then
        sleep_for="$remaining"
    fi
    echo "Health endpoints are not both ready; retrying in ${sleep_for}s."
    sleep "$sleep_for"
done

[[ "$api_healthy" == true ]] \
    || echo "API health endpoint failed readiness after ${HEALTH_TIMEOUT_SECONDS}s: ${API_URL}" >&2
[[ "$nginx_healthy" == true ]] \
    || echo "Nginx health endpoint failed readiness after ${HEALTH_TIMEOUT_SECONDS}s: ${NGINX_URL}" >&2
exit 1
