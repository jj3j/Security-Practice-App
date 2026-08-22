#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$(uname -s)" == MINGW* ]]; then
    export MSYS=winsymlinks:sys
fi


readonly REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly TEST_ROOT="$(mktemp -d /tmp/gdsa-deployment-tests.XXXXXX)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

fail() {
    echo "Deployment regression test failed: $*" >&2
    exit 1
}

assert_file() {
    [[ -f "$1" ]] || fail "expected file: $1"
}

assert_directory() {
    [[ -d "$1" && ! -L "$1" ]] || fail "expected ordinary directory: $1"
}

assert_missing() {
    [[ ! -e "$1" && ! -L "$1" ]] || fail "expected path to be absent: $1"
}

assert_symlink_target() {
    local actual
    actual="$(readlink -f "$1")"
    [[ "$actual" == "$2" ]] || fail "expected $1 -> $2, got $actual"
}

content_digest() {
    find "$1" -type f -print0 \
        | sort -z \
        | xargs -0 sha256sum \
        | sha256sum \
        | awk '{print $1}'
}

make_fake_curl() {
    local fake_bin="$1"
    mkdir -p "$fake_bin"
    cat > "$fake_bin/curl" <<'CURL'
#!/usr/bin/env bash
set -euo pipefail
url="${!#}"
if [[ "${CURL_MODE:-success}" == "fail" ]]; then
    echo "curl: (7) simulated connection refused for ${url}" >&2
    exit 7
fi
if [[ "$url" == *api* ]]; then
    count_file="${CURL_STATE_DIR}/api-count"
    count=0
    [[ ! -f "$count_file" ]] || count="$(<"$count_file")"
    count=$((count + 1))
    echo "$count" > "$count_file"
    if (( count < 3 )); then
        echo "curl: (7) simulated API startup delay" >&2
        exit 7
    fi
else
    echo 1 > "${CURL_STATE_DIR}/nginx-count"
fi
printf '{"ok":true}'
CURL
    chmod +x "$fake_bin/curl"
}

test_health_readiness() {
    local case_root="$TEST_ROOT/health"
    local output="$case_root/output"
    local start elapsed status
    mkdir -p "$case_root/state"
    make_fake_curl "$case_root/bin"

    PATH="$case_root/bin:$PATH" \
    CURL_STATE_DIR="$case_root/state" \
    GDSA_API_HEALTH_URL="http://api/health" \
    GDSA_NGINX_HEALTH_URL="http://nginx/health" \
    GDSA_HEALTH_TIMEOUT_SECONDS=5 \
    GDSA_HEALTH_RETRY_INTERVAL_SECONDS=1 \
        bash "$REPOSITORY_ROOT/deploy/scripts/health-check.sh" > "$output" 2>&1

    [[ "$(<"$case_root/state/api-count")" == 3 ]] \
        || fail "API readiness did not retry twice before succeeding"
    [[ "$(<"$case_root/state/nginx-count")" == 1 ]] \
        || fail "healthy Nginx endpoint was unnecessarily retried"
    grep -Fq "Deployment health checks passed." "$output" \
        || fail "health success diagnostic is missing"

    start=$SECONDS
    set +e
    PATH="$case_root/bin:$PATH" \
    CURL_MODE=fail \
    CURL_STATE_DIR="$case_root/state" \
    GDSA_API_HEALTH_URL="http://api/health" \
    GDSA_NGINX_HEALTH_URL="http://nginx/health" \
    GDSA_HEALTH_TIMEOUT_SECONDS=2 \
    GDSA_HEALTH_RETRY_INTERVAL_SECONDS=1 \
        bash "$REPOSITORY_ROOT/deploy/scripts/health-check.sh" > "$output" 2>&1
    status=$?
    set -e
    elapsed=$((SECONDS - start))

    [[ "$status" -ne 0 ]] || fail "unhealthy endpoints unexpectedly passed"
    (( elapsed >= 2 && elapsed <= 4 )) \
        || fail "health timeout was not bounded near two seconds (elapsed=${elapsed})"
    grep -Fq "API health endpoint failed readiness" "$output" \
        || fail "API failure diagnostic is missing"
    grep -Fq "Nginx health endpoint failed readiness" "$output" \
        || fail "Nginx failure diagnostic is missing"
    grep -Fq "curl: (7) simulated connection refused" "$output" \
        || fail "final curl diagnostics were hidden"
}

make_release_archive() {
    local archive="$1"
    local source_dir="$TEST_ROOT/archive-source"
    rm -rf -- "$source_dir"
    mkdir -p "$source_dir/backend" "$source_dir/frontend" "$source_dir/deploy/scripts"
    : > "$source_dir/backend/requirements.txt"
    : > "$source_dir/backend/practice_api.py"
    : > "$source_dir/frontend/index.html"
    : > "$source_dir/deploy/scripts/validate-study-content.py"
    cat > "$source_dir/deploy/scripts/health-check.sh" <<'HEALTH'
#!/usr/bin/env bash
[[ "${DEPLOY_TEST_HEALTH_RESULT:-pass}" == pass ]]
HEALTH
    chmod +x "$source_dir/deploy/scripts/health-check.sh"
    tar --create --gzip --file "$archive" --directory "$source_dir" .
}

make_deployment_harness() {
    local case_root="$1"
    local deploy_root="$case_root/security-study"
    local env_file="$case_root/gdsa-practice.env"

    mkdir -p "$case_root/fake-bin" "$deploy_root/data" "$deploy_root/venv/bin"
    : > "$env_file"
    : > "$deploy_root/data/gdsa-practice.sqlite3"
    : > "$deploy_root/data/projects_index.jsonl"

    cat > "$deploy_root/venv/bin/python" <<'PYTHON'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == -m && "${2:-}" == pip ]]; then
    exit 0
fi
target="${!#}"
[[ -f "$target/.valid" ]]
PYTHON
    chmod +x "$deploy_root/venv/bin/python"
    cat > "$deploy_root/venv/bin/gunicorn" <<'GUNICORN'
#!/usr/bin/env bash
GUNICORN
    chmod +x "$deploy_root/venv/bin/gunicorn"

    cat > "$case_root/fake-bin/chown" <<'CHOWN'
#!/usr/bin/env bash
exit 0
CHOWN
    cat > "$case_root/fake-bin/install" <<'INSTALL'
#!/usr/bin/env bash
set -euo pipefail
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        -d) shift ;;
        -o|-g|-m) shift 2 ;;
        *) mkdir -p -- "$1"; shift ;;
    esac
done
INSTALL
    chmod +x "$case_root/fake-bin/install"

    cat > "$case_root/fake-bin/runuser" <<'RUNUSER'
#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == --user && "$2" == gdsa-practice && "$3" == -- && "$4" == test && "$5" == -r && -r "$6" ]]
RUNUSER
    cat > "$case_root/fake-bin/systemctl" <<'SYSTEMCTL'
#!/usr/bin/env bash
exit 0
SYSTEMCTL
    chmod +x "$case_root/fake-bin/"*

    sed \
        -e 's/if \[\[ "${EUID}" -ne 0 \]\]; then/if false; then/' \
        -e "s|readonly DEPLOY_ROOT=\"/opt/security-study\"|readonly DEPLOY_ROOT=\"$deploy_root\"|" \
        -e "s|readonly ENV_FILE=\"/etc/security-study/gdsa-practice.env\"|readonly ENV_FILE=\"$env_file\"|" \
        "$REPOSITORY_ROOT/deploy/scripts/deploy-release.sh" > "$case_root/deploy-release.sh"
    chmod +x "$case_root/deploy-release.sh"
}

seed_ordinary_current() {
    local current="$1"
    mkdir -p "$current/backend" "$current/frontend" "$current/content"
    : > "$current/backend/practice_api.py"
    : > "$current/frontend/index.html"
    : > "$current/content/.valid"
    for name in sec530-study.json cissp-study.json cissp-study-sources.jsonl gmon-study.json gmon-study-sources.jsonl; do
        printf 'original-%s\n' "$name" > "$current/content/$name"
    done
}

run_deploy() {
    local case_root="$1"
    local release_id="$2"
    local health_result="$3"
    local adoption_flag="${4:-}"
    local -a deploy_args=()
    local archive="/tmp/gdsa-practice-${release_id}.tar.gz"
    if [[ -n "$adoption_flag" ]]; then
        deploy_args+=("$adoption_flag")
    fi
    make_release_archive "$archive"
    PATH="$case_root/fake-bin:$PATH" \
    DEPLOY_TEST_HEALTH_RESULT="$health_result" \
        bash "$case_root/deploy-release.sh" "${deploy_args[@]}" "$archive" "$release_id"
}

test_adoption_retry_and_cleanup() {
    local case_root="$TEST_ROOT/adoption"
    local deploy_root="$case_root/security-study"
    local persistent="$deploy_root/data/content"
    local digest_after_failure
    local status
    mkdir -p "$case_root"
    make_deployment_harness "$case_root"
    seed_ordinary_current "$deploy_root/current"
    set +e
    run_deploy "$case_root" adoption-gate pass
    status=$?
    set -e
    [[ "$status" -ne 0 ]] || fail "ordinary current bypassed the explicit adoption gate"
    assert_directory "$deploy_root/current"
    assert_missing "$deploy_root/data/content"
    assert_missing "$deploy_root/releases/adoption-gate"
    rm -f -- /tmp/gdsa-practice-adoption-gate.tar.gz


    set +e
    run_deploy "$case_root" failed-adoption fail --adopt-existing-current
    status=$?
    set -e
    [[ "$status" -ne 0 ]] || fail "failed adoption unexpectedly succeeded"
    assert_directory "$deploy_root/current"
    assert_directory "$persistent"
    assert_missing "$deploy_root/releases/failed-adoption"
    assert_missing "$deploy_root/releases/baseline-before-failed-adoption"
    digest_after_failure="$(content_digest "$persistent")"

    mv -- "$persistent/.valid" "$case_root/persistent-valid-marker"
    set +e
    run_deploy "$case_root" invalid-persistent pass --adopt-existing-current
    status=$?
    set -e
    [[ "$status" -ne 0 ]] || fail "invalid persistent content unexpectedly passed"
    assert_directory "$deploy_root/current"
    assert_directory "$persistent"
    assert_missing "$deploy_root/releases/invalid-persistent"
    mv -- "$case_root/persistent-valid-marker" "$persistent/.valid"
    [[ "$(content_digest "$persistent")" == "$digest_after_failure" ]] \
        || fail "invalid-content failure modified persistent content"

    printf 'changed-source\n' > "$deploy_root/current/content/sec530-study.json"
    run_deploy "$case_root" adopted pass --adopt-existing-current
    assert_symlink_target "$deploy_root/current" "$deploy_root/releases/adopted"
    assert_directory "$deploy_root/releases/baseline-before-adopted"
    [[ "$(content_digest "$persistent")" == "$digest_after_failure" ]] \
        || fail "adoption retry overwrote persistent content"

    run_deploy "$case_root" normal pass
    assert_symlink_target "$deploy_root/current" "$deploy_root/releases/normal"
    assert_directory "$deploy_root/releases/adopted"
    assert_directory "$deploy_root/releases/baseline-before-adopted"

    set +e
    run_deploy "$case_root" failed-normal fail
    status=$?
    set -e
    [[ "$status" -ne 0 ]] || fail "unhealthy normal release unexpectedly succeeded"
    assert_symlink_target "$deploy_root/current" "$deploy_root/releases/normal"
    assert_missing "$deploy_root/releases/failed-normal"
    assert_directory "$deploy_root/releases/normal"
    assert_directory "$deploy_root/releases/adopted"
    assert_directory "$deploy_root/releases/baseline-before-adopted"
    [[ "$(content_digest "$persistent")" == "$digest_after_failure" ]] \
        || fail "rollback modified persistent content"
}

test_health_readiness
test_adoption_retry_and_cleanup
echo "Deployment regression tests passed."
