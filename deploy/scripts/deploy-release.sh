#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "This deployment script must run as root (normally through sudo)." >&2
    exit 1
fi

if [[ "$#" -ne 2 ]]; then
    echo "Usage: deploy-release.sh ARCHIVE RELEASE_ID" >&2
    exit 2
fi

readonly ARCHIVE="$1"
readonly RELEASE_ID="$2"
readonly DEPLOY_ROOT="/opt/gdsa-practice"
readonly SERVICE_NAME="gdsa-practice.service"
readonly ENV_FILE="/etc/gdsa-practice/gdsa-practice.env"
readonly RELEASES_DIR="${DEPLOY_ROOT}/releases"
readonly CURRENT_LINK="${DEPLOY_ROOT}/current"
readonly VENV_DIR="${DEPLOY_ROOT}/venv"
readonly RELEASE_DIR="${RELEASES_DIR}/${RELEASE_ID}"

die() {
    echo "Deployment failed: $*" >&2
    exit 1
}

if [[ ! "$RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    die "release ID contains unsupported characters"
fi
if [[ "$DEPLOY_ROOT" != /* || "$DEPLOY_ROOT" == "/" || "$DEPLOY_ROOT" == *..* ]]; then
    die "deployment root must be an absolute path without '..': ${DEPLOY_ROOT}"
fi
case "$ARCHIVE" in
    /tmp/gdsa-practice-*.tar.gz) ;;
    *) die "archive must be a release archive under /tmp" ;;
esac
[[ -f "$ARCHIVE" ]] || die "release archive not found: ${ARCHIVE}"
[[ ! -e "$RELEASE_DIR" ]] || die "release already exists: ${RELEASE_DIR}"
[[ -f "$ENV_FILE" ]] || die "missing production environment file: ${ENV_FILE}"
[[ -f "${DEPLOY_ROOT}/data/gdsa-practice.sqlite3" ]] || die "missing question database"
[[ -f "${DEPLOY_ROOT}/data/projects_index.jsonl" ]] || die "missing project-aware RAG index"

readonly STAGING_DIR="$(mktemp -d /tmp/gdsa-practice-release.XXXXXX)"
cleanup() {
    rm -rf -- "$STAGING_DIR"
    rm -f -- "$ARCHIVE"
}
trap cleanup EXIT

while IFS= read -r member; do case "$member" in /*|../*|*/../*|..|*/..) die "unsafe archive member: ${member}" ;; esac; done < <(tar --list --gzip --file "$ARCHIVE")

tar --extract --gzip --file "$ARCHIVE" --directory "$STAGING_DIR" --no-same-owner --no-same-permissions
for required_path in backend/requirements.txt backend/practice_api.py frontend/index.html content; do [[ -e "$STAGING_DIR/$required_path" ]] || die "release archive is missing ${required_path}"; done

install -d -o root -g root -m 0755 "$DEPLOY_ROOT" "$RELEASES_DIR"
install -d -o root -g root -m 0755 "$RELEASE_DIR"
for item in backend frontend content deploy scripts docs README.md .gitattributes; do if [[ -e "$STAGING_DIR/$item" ]]; then cp -a -- "$STAGING_DIR/$item" "$RELEASE_DIR/"; fi; done
chown -R root:root "$RELEASE_DIR"
find "$RELEASE_DIR" -type d -exec chmod 0755 {} +
find "$RELEASE_DIR" -type f -exec chmod 0644 {} +

command -v python3 >/dev/null 2>&1 || die "python3 is not installed"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR" || die "failed to create Python virtual environment: ${VENV_DIR}"
fi

"$VENV_DIR/bin/python" -m pip install \
    --disable-pip-version-check \
    --requirement "$RELEASE_DIR/backend/requirements.txt" \
    || die "failed to install Python dependencies"

[[ -x "$VENV_DIR/bin/gunicorn" ]] \
    || die "Gunicorn was not installed into virtual environment: ${VENV_DIR}"

previous_release=""
if [[ -L "$CURRENT_LINK" ]]; then
    previous_release="$(readlink -f "$CURRENT_LINK" || true)"
elif [[ -e "$CURRENT_LINK" ]]; then
    die "current deployment path is not a symlink: ${CURRENT_LINK}"
fi

rm -f -- "${CURRENT_LINK}.next"
ln -s -- "$RELEASE_DIR" "${CURRENT_LINK}.next"
mv -Tf -- "${CURRENT_LINK}.next" "$CURRENT_LINK"

rollback() {
    if [[ -n "$previous_release" && -d "$previous_release" ]]; then
        rm -f -- "${CURRENT_LINK}.next"
        ln -s -- "$previous_release" "${CURRENT_LINK}.next"
        mv -Tf -- "${CURRENT_LINK}.next" "$CURRENT_LINK"
        systemctl restart "$SERVICE_NAME" || true
    fi
}

if ! systemctl restart "$SERVICE_NAME"; then
    rollback
    die "systemd restart failed; previous release was restored when available"
fi
if ! bash "$RELEASE_DIR/deploy/scripts/health-check.sh"; then
    rollback
    die "health check failed; previous release was restored when available"
fi

echo "Deployment succeeded: ${RELEASE_ID}"
