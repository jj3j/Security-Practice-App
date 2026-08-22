# GitHub Actions and EC2 deployment

The repository has two workflows:

- `.github/workflows/ci.yml` validates the repository on pushes and pull
  requests.
- `.github/workflows/deploy-ec2.yml` runs after CI for pushes to `main`, or
  manually through **Actions → Deploy to EC2 → Run workflow**.

The deployment workflow uploads an archive of the committed tree. CI verifies
that the archive contains application/runtime scripts and excludes `content/`.
The server keeps immutable releases under `/opt/security-study/releases` and
atomically switches `/opt/security-study/current`. Persistent study content,
runtime data, the virtual environment, the learner database, and the populated
environment file stay outside releases.

## Safety gate before secrets

Do not configure any GitHub EC2 secret until all of these are validated:

- the checked-in paths match the active `/opt/security-study` layout;
- the checked-in systemd unit is reconciled with the active unit and override;
- active Nginx-only cookie/security customizations have a preserve-and-merge
  plan;
- the ordinary `/opt/security-study/current` directory passes the backend and
  frontend preflight; and
- the one-time adoption and restoration procedure in
  `docs/EC2_DEPLOYMENT.md` has been reviewed.

The backup service and timer are currently absent. CI/CD readiness does not
prove that backups are active.

## Required GitHub configuration

After the safety gate is complete, add these as repository or `production`
environment secrets:

| Secret | Value |
|---|---|
| `EC2_HOST` | EC2 DNS name or IP address |
| `EC2_USER` | SSH deployment user (`ubuntu` on the verified host) |
| `EC2_SSH_PRIVATE_KEY` | Private key matching that user |
| `EC2_KNOWN_HOSTS` | Independently verified host-key entry |
| `EC2_SSH_PORT` | Optional SSH port; defaults to `22` |

Never commit the key, disable strict host-key checking, or expose secret
values.

## Deployment behavior

The remote script requires:

- `/etc/security-study/gdsa-practice.env`;
- `/opt/security-study/venv/bin/gunicorn`;
- `/opt/security-study/data/content`, or a valid first-migration source in
  `current/content`, with all required catalogs and source indexes;
- `/opt/security-study/data/gdsa-practice.sqlite3`; and
- `/opt/security-study/data/projects_index.jsonl`.

An automatic push deployment never opts into migration. While `current` is an
ordinary directory it will fail safely. For the one-time conversion, manually
dispatch the workflow with `adopt_existing_current` enabled. If persistent
content does not exist yet, the script first validates and copies the existing
`current/content` without deleting it. It then validates the persistent bundle,
links it into the new release, validates the existing backend and frontend,
preserves the entire directory as a baseline release, activates the new release
as a symlink, and restores the ordinary directory if activation, restart, or
health checks fail.

After successful adoption, leave the input disabled. Normal deployments switch
the `current` symlink atomically, restart `gdsa-practice.service`, check both
Gunicorn and Nginx health endpoints, and restore the previous symlink on
failure. Deployments do not import question banks, rebuild the RAG index, or
modify the learner database or study content.
