# EC2 bootstrap and operations

This repository targets the existing Ubuntu EC2 deployment at
`/opt/security-study`. Nginx serves the frontend and proxies `/health`,
`/api/`, and `/auth/` to Gunicorn at `127.0.0.1:8765`. The service remains
`gdsa-practice.service` and runs as `gdsa-practice`.

## Public routing through Cloudflare Tunnel

`study.querytm.com` routes through the existing Cloudflare Tunnel `whalebot`
(tunnel ID `468dcf5e-006a-4f87-a778-e61f9dfd15e9`). In
`/etc/cloudflared/config.yml`, the Study App origin is:

```text
http://127.0.0.1:80
```

Port `8765` is the Gunicorn backend only; direct requests to it return
`{"error":"Endpoint not found."}`. Nginx listens on port `80`, serves the
Study App frontend, and proxies backend traffic to `127.0.0.1:8765`.

The public URL is working at `https://study.querytm.com`. The cloudflared
service is enabled and active.

Preserve the existing WhaleBot route exactly:

```text
whale.querytm.com -> http://127.0.0.1:8766
```

Do not configure the GitHub EC2 secrets yet. First validate this checked-in
contract and the one-time `current` migration procedure against the instance.
Server provisioning and migration are deliberate administrative operations;
they are not performed by updating this repository.

## Runtime contract

The required paths are:

```text
/opt/security-study/current
/opt/security-study/releases
/opt/security-study/data/content
/opt/security-study/venv
/opt/security-study/data/gdsa-practice.sqlite3
/opt/security-study/data/projects_index.jsonl
/etc/security-study/gdsa-practice.env
/var/lib/gdsa-practice/learner.sqlite3
```

Application release archives do not contain `content/` or either SQLite
database. Each release instead contains a `content` symlink to
`/opt/security-study/data/content`. During deployment, the committed GMON bank
is atomically reconciled into the persistent question database while every
other course bank is preserved. The learner database, SEC530 RAG index, virtual
environment, and populated environment file are not replaced. The application
tree and question database are read-only to the service; learner state remains
writable under `/var/lib/gdsa-practice` through
`StateDirectory=gdsa-practice`.

For a new host, create the runtime account and directories with the same
ownership and permissions as the active deployment:

```bash
sudo useradd --system --home-dir /opt/security-study --shell /usr/sbin/nologin gdsa-practice
sudo install -d -o root -g root -m 0755 /opt/security-study/releases
sudo install -d -o root -g gdsa-practice -m 0750 /opt/security-study/data
sudo install -d -o root -g root -m 0750 /etc/security-study
sudo install -d -o root -g root -m 0750 /var/lib/gdsa-practice
sudo python3 -m venv /opt/security-study/venv
```

If the account or directories already exist, inspect them instead of recreating
or changing them blindly.

## Runtime data and environment

Install validated, read-only runtime data at:

```text
/opt/security-study/data/content/sec530-study.json
/opt/security-study/data/content/cissp-study.json
/opt/security-study/data/content/cissp-study-sources.jsonl
/opt/security-study/data/content/gmon-study.json
/opt/security-study/data/content/gmon-study-sources.jsonl
/opt/security-study/data/gdsa-practice.sqlite3
/opt/security-study/data/projects_index.jsonl
```

Provision study content independently of Git releases. Keep the content files
owned by `root:gdsa-practice`, with directories mode `0750` and files mode
`0640`. The deployment validates all three catalogs and the CISSP/GMON source
indexes before activating a release. It also validates and idempotently merges
the committed 328-question GMON bank into the question database. It does not
modify study content.
Existing environment values under `/opt/security-study/current/content/...`
remain valid because every release links that path to persistent storage.

For the first adoption only, leave `/opt/security-study/data/content` absent if
the deployment should copy the validated existing `current/content`. An empty
or incomplete persistent directory fails validation and is never filled from a
release implicitly.

The populated environment file belongs at
`/etc/security-study/gdsa-practice.env`, owned by
`root:gdsa-practice` with mode `0640`. Do not replace an existing populated
file with the example and never commit its values. Preserve
`GDSA_LEARNER_DATABASE_PATH=/var/lib/gdsa-practice/learner.sqlite3`.

## Reconcile systemd and Nginx

The checked-in systemd unit matches the active base/effective override contract:

- `User=gdsa-practice` and `Group=gdsa-practice`;
- `StateDirectory=gdsa-practice`;
- `ReadOnlyPaths=/opt/security-study`;
- working directory and Gunicorn paths under `/opt/security-study`; and
- environment file `/etc/security-study/gdsa-practice.env`.

Before installing it, compare it with both
`/etc/systemd/system/gdsa-practice.service` and
`/etc/systemd/system/gdsa-practice.service.d/override.conf`. Preserve any
additional active hardening that does not conflict with this contract.

The active Nginx source is
`/etc/nginx/sites-available/gdsa-practice.sites-available.conf`, enabled by
`/etc/nginx/sites-enabled/gdsa-practice`. Do not blindly overwrite the active
source with the repository template: it may contain cookie or security
customizations that were not captured during inspection. Compare and merge the
required document-root change to
`/opt/security-study/current/frontend` while preserving the active routes,
headers, and cookie/security behavior. Validate the merged configuration with
`sudo nginx -t` before any reload.

The checked-in backup service and timer are templates only.
`gdsa-practice-backup.service` and its timer are not currently installed or
active on the verified EC2 host. Do not claim backup coverage or enable the
timer until the service, destination, retention, and a restore test have been
separately reviewed.

## One-time adoption of the ordinary current directory

The verified host currently has an ordinary
`/opt/security-study/current` directory, including manual `backend-bak`,
`frontend-bak`, and `content-bak` directories. Normal deployments refuse to
replace it. After the contract and rollback procedure have been validated,
configure the GitHub secrets and manually run **Deploy to EC2** with
`adopt_existing_current` enabled exactly once.

The deployment script then:

1. verifies that `current/backend/practice_api.py` and
   `current/frontend/index.html` exist;
2. builds the new immutable release under
   `/opt/security-study/releases/<release-id>`;
3. validates and atomically reconciles the committed GMON bank into the
   persistent question database while preserving every other course bank;
4. if persistent content does not exist, validates and copies the existing
   `current/content` into `/opt/security-study/data/content`, leaving the
   original copy untouched;
   if persistent content already exists after a failed attempt, validates and
   reuses it without overwriting or repeating the migration;
5. validates all required persistent study catalogs and indexes;
6. links the release's `content` path to persistent storage;
7. moves the complete ordinary `current` directory, including its backup
   directories, intact to
   `/opt/security-study/releases/baseline-before-<release-id>`;
8. atomically activates the new release through the `current` symlink;
9. restarts `gdsa-practice.service` so catalogs are reloaded; and
10. polls both `http://127.0.0.1:8765/health` and
   `http://127.0.0.1/health` every two seconds until both are ready or the
   shared 30-second readiness deadline expires.

If activation, restart, or either health check fails, the new `current`
symlink is removed and the baseline directory is moved back to the original
ordinary `/opt/security-study/current` path. After a restart or health failure,
the script also attempts to restart the restored application. Once rollback is
confirmed, only the newly failed release is removed. The script never removes
the restored production release, the adoption baseline, the release referenced
by `current`, or persistent data. If rollback safety cannot be confirmed, the
failed release is left in place and the deployment reports why. The GMON bank
reconciliation may remain after an application rollback; it is idempotent and
does not alter other course banks. The learner database and persistent study
content are untouched.

On success, `current` is a symlink to the new immutable release and the
baseline remains in `releases`. Do not enable the adoption input again.
Later deployments atomically switch the symlink, run both health checks, and
restore the previous release symlink on failure.

## Verification and manual rollback

After an authorized deployment, verify:

```bash
sudo systemctl status gdsa-practice.service --no-pager
curl --fail --silent --show-error http://127.0.0.1:8765/health
curl --fail --silent --show-error http://127.0.0.1/health
sudo ss -ltnp | grep -E ':(80|8765)\b'
readlink -f /opt/security-study/current
```

Port `8765` must remain bound only to `127.0.0.1`.

To roll back a later symlink-based release:

```bash
PREVIOUS_RELEASE=/opt/security-study/releases/<verified-release-id>
sudo test -f "$PREVIOUS_RELEASE/backend/practice_api.py"
sudo test -f "$PREVIOUS_RELEASE/frontend/index.html"
sudo ln -s "$PREVIOUS_RELEASE" /opt/security-study/current.next
sudo mv -Tf /opt/security-study/current.next /opt/security-study/current
sudo systemctl restart gdsa-practice.service
```

Application rollback does not modify the learner database or persistent study
content. The compatible GMON bank reconciliation remains in the question
database and is safely reused by the next deployment attempt.
