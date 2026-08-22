#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$#" -ne 1 ]]; then
    echo "Usage: validate-release-archive.sh ARCHIVE" >&2
    exit 2
fi

readonly ARCHIVE="$1"
[[ -f "$ARCHIVE" ]] || { echo "Release archive not found: ${ARCHIVE}" >&2; exit 1; }

members="$(tar --list --gzip --file "$ARCHIVE")"

while IFS= read -r member; do
    case "$member" in
        /*|../*|*/../*|..|*/..)
            echo "Unsafe release archive member: ${member}" >&2
            exit 1
            ;;
        content|content/|content/*)
            echo "Release archive must not contain persistent study content: ${member}" >&2
            exit 1
            ;;
    esac
done <<< "$members"

for required_path in \
    backend/requirements.txt \
    backend/practice_api.py \
    frontend/index.html \
    deploy/scripts/deploy-release.sh \
    deploy/scripts/health-check.sh \
    deploy/scripts/validate-study-content.py; do
    if ! grep -Fxq "$required_path" <<< "$members"; then
        echo "Release archive is missing ${required_path}" >&2
        exit 1
    fi
done

echo "Release archive validation passed."
