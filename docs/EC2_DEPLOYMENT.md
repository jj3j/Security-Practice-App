# EC2 bootstrap and operations

This repository assumes an Ubuntu EC2 instance with Nginx in front of Gunicorn.
The API binds only to `127.0.0.1:8765`; the public web server serves the static
frontend and proxies `/health`, `/api/`, and `/auth/` to Gunicorn.

The GitHub workflow handles later application releases. One-time server
provisioning remains a deliberate administrative step.

## 1. Prepare the server

Install the operating-system packages and create the service account:

```bash
sudo apt-get update
sudo apt-get install --yes nginx python3 python3-venv curl
sudo useradd --system --home-dir /opt/gdsa-practice --shell /usr/sbin/nologin gdsa-practice
sudo install -d -o root -g root -m 0755 /opt/gdsa-practice/releases
sudo install -d -o root -g gdsa-practice -m 0750 /opt/gdsa-practice/data
sudo install -d -o root -g root -m 0750 /etc/gdsa-practice
sudo install -d -o root -g root -m 0750 /var/backups/gdsa-practice
sudo install -d -o root -g root -m 0750 /var/lib/gdsa-practice
sudo python3 -m venv /opt/gdsa-practice/venv
```

If the user already exists, verify that it is a locked system account using
`/usr/sbin/nologin` before continuing.

## 2. Install the runtime data

The Git repository contains application code and immutable study content. It
does not contain the runtime question database or the project-aware RAG index.
Install validated copies at:

```text
/opt/gdsa-practice/data/gdsa-practice.sqlite3
/opt/gdsa-practice/data/projects_index.jsonl
```

Set permissions so the service can read them without making them writable:

```bash
sudo chown root:gdsa-practice /opt/gdsa-practice/data/gdsa-practice.sqlite3
sudo chmod 0640 /opt/gdsa-practice/data/gdsa-practice.sqlite3
sudo chown root:gdsa-practice /opt/gdsa-practice/data/projects_index.jsonl
sudo chmod 0640 /opt/gdsa-practice/data/projects_index.jsonl
```

Do not upload source PDFs, authoring directories, SSH keys, or populated
environment files as part of an application release.

## 3. Configure secrets and system services

Copy `deploy/environment/gdsa-practice.env.example` to the server and set the
Google OAuth values and public HTTPS origin:

```bash
sudo install -o root -g gdsa-practice -m 0640 \
  deploy/environment/gdsa-practice.env.example \
  /etc/gdsa-practice/gdsa-practice.env
sudoedit /etc/gdsa-practice/gdsa-practice.env
```

Install the service and Nginx configuration from a checked-out or securely
transferred copy of this repository:

```bash
sudo install -o root -g root -m 0644 \
  deploy/systemd/gdsa-practice.service \
  /etc/systemd/system/gdsa-practice.service
sudo install -o root -g root -m 0644 \
  deploy/systemd/gdsa-practice-backup.service \
  /etc/systemd/system/gdsa-practice-backup.service
sudo install -o root -g root -m 0644 \
  deploy/systemd/gdsa-practice-backup.timer \
  /etc/systemd/system/gdsa-practice-backup.timer
sudo install -o root -g root -m 0644 \
  deploy/nginx/gdsa-practice.sites-available.conf \
  /etc/nginx/sites-available/gdsa-practice
sudo ln -sfn /etc/nginx/sites-available/gdsa-practice \
  /etc/nginx/sites-enabled/gdsa-practice
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable gdsa-practice.service
sudo systemctl enable --now gdsa-practice-backup.timer
```

The Nginx configuration includes `/etc/nginx/proxy_params`, which is present on
the standard Ubuntu Nginx package. Add HTTPS before exposing the service to an
untrusted network; Google login requires the production HTTPS origin configured
in the environment file.

## 4. Run the first release

After the server is provisioned and the GitHub secrets are configured, merge or
push a commit to `main`. The deployment workflow will refuse to proceed until
the runtime prerequisites above exist. Verify the result on the instance:

```bash
sudo systemctl status gdsa-practice.service --no-pager
curl --fail --silent --show-error http://127.0.0.1:8765/health
curl --fail --silent --show-error http://127.0.0.1/health
sudo ss -ltnp | grep -E ':(80|8765)\b'
```

Port `8765` should be bound only to `127.0.0.1`. Do not add it to the EC2
security group. Restrict SSH to an administrative IP range and add HTTPS only
after TLS and access-control decisions are complete.

## 5. Roll back an application release

The deployment script keeps old releases. To restore one manually:

```bash
PREVIOUS_RELEASE=/opt/gdsa-practice/releases/<verified-release-id>
sudo test -d "$PREVIOUS_RELEASE/backend"
sudo ln -s "$PREVIOUS_RELEASE" /opt/gdsa-practice/current.next
sudo mv -Tf /opt/gdsa-practice/current.next /opt/gdsa-practice/current
sudo systemctl restart gdsa-practice.service
```

The release rollback does not modify either SQLite database.
