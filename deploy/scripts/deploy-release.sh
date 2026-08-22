#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "This deployment script must run as root (normally through sudo)." >&2
    exit 1
fi

adopt_existing_current=false
if [[ "${1:-}" == "--adopt-existing-current" ]]; then
    adopt_existing_current=true
    shift
fi

if [[ "$#" -ne 2 ]]; then
    echo "Usage: deploy-release.sh [--adopt-existing-current] ARCHIVE RELEASE_ID" >&2
    exit 2
fi

readonly ARCHIVE="$1"
readonly RELEASE_ID="$2"
readonly DEPLOY_ROOT="/opt/security-study"
readonly SERVICE_NAME="gdsa-practice.service"
readonly ENV_FILE="/etc/security-study/gdsa-practice.env"
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
[[ ! -e "$RELEASE_DIR" && ! -L "$RELEASE_DIR" ]] || die "release already exists: ${RELEASE_DIR}"
[[ -f "$ENV_FILE" ]] || die "missing production environment file: ${ENV_FILE}"
[[ -f "${DEPLOY_ROOT}/data/gdsa-practice.sqlite3" ]] || die "missing question database"
[[ -f "${DEPLOY_ROOT}/data/projects_index.jsonl" ]] || die "missing project-aware RAG index"

previous_release=""
baseline_release=""
current_kind="absent"
if [[ -L "$CURRENT_LINK" ]]; then
    [[ "$adopt_existing_current" == false ]] \
        || die "--adopt-existing-current requires an existing ordinary directory: ${CURRENT_LINK}"
    previous_release="$(readlink -f "$CURRENT_LINK" || true)"
    [[ -n "$previous_release" && -d "$previous_release" ]] \
        || die "current symlink does not resolve to a release directory: ${CURRENT_LINK}"
    current_kind="symlink"
elif [[ -d "$CURRENT_LINK" ]]; then
    [[ "$adopt_existing_current" == true ]] \
        || die "current is an ordinary directory; rerun once with --adopt-existing-current after validating the migration procedure"
    for existing_path in backend/practice_api.py frontend/index.html; do
        [[ -f "$CURRENT_LINK/$existing_path" ]] \
            || die "existing current directory is missing ${existing_path}; refusing adoption"
    done
    baseline_release="${RELEASES_DIR}/baseline-before-${RELEASE_ID}"
    [[ ! -e "$baseline_release" && ! -L "$baseline_release" ]] \
        || die "baseline release already exists: ${baseline_release}"
    current_kind="directory"
elif [[ -e "$CURRENT_LINK" ]]; then
    die "current deployment path is neither a directory nor a symlink: ${CURRENT_LINK}"
elif [[ "$adopt_existing_current" == true ]]; then
    die "--adopt-existing-current requires an existing ordinary directory: ${CURRENT_LINK}"
fi

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

if [[ "$current_kind" == "directory" ]]; then
    mv -- "$CURRENT_LINK" "$baseline_release" \
        || die "failed to preserve the existing current directory as ${baseline_release}"
fi

restore_previous() {
    rm -f -- "${CURRENT_LINK}.next"

    if [[ -n "$baseline_release" && -d "$baseline_release" ]]; then
        if [[ -L "$CURRENT_LINK" ]]; then
            rm -f -- "$CURRENT_LINK" || return 1
        elif [[ -e "$CURRENT_LINK" ]]; then
            echo "Refusing to overwrite unexpected path while restoring: ${CURRENT_LINK}" >&2
            return 1
        fi
        mv -- "$baseline_release" "$CURRENT_LINK"
    elif [[ -n "$previous_release" && -d "$previous_release" ]]; then
        ln -s -- "$previous_release" "${CURRENT_LINK}.next" || return 1
        mv -Tf -- "${CURRENT_LINK}.next" "$CURRENT_LINK"
    else
        if [[ -L "$CURRENT_LINK" ]]; then
            rm -f -- "$CURRENT_LINK"
        elif [[ -e "$CURRENT_LINK" ]]; then
            echo "Refusing to remove unexpected path while rolling back: ${CURRENT_LINK}" >&2
            return 1
        fi
    fi
}

rm -f -- "${CURRENT_LINK}.next"
if ! ln -s -- "$RELEASE_DIR" "${CURRENT_LINK}.next" \
    || ! mv -Tf -- "${CURRENT_LINK}.next" "$CURRENT_LINK"; then
    restore_previous || die "release activation failed and the previous current path could not be restored"
    die "release activation failed; the previous current path was restored"
fi

if ! systemctl restart "$SERVICE_NAME"; then
    restore_previous || die "systemd restart failed and the previous current path could not be restored"
    if [[ "$current_kind" != "absent" ]]; then
        systemctl restart "$SERVICE_NAME" || true
    fi
    die "systemd restart failed; the previous current path was restored"
fi
if ! bash "$RELEASE_DIR/deploy/scripts/health-check.sh"; then
    restore_previous || die "health check failed and the previous current path could not be restored"
    if [[ "$current_kind" != "absent" ]]; then
        systemctl restart "$SERVICE_NAME" || true
    fi
    die "health check failed; the previous current path was restored"
fi

if [[ -n "$baseline_release" ]]; then
    echo "Adopted previous current directory as baseline release: ${baseline_release}"
fi
echo "Deployment succeeded: ${RELEASE_ID}"
