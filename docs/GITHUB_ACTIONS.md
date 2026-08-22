# GitHub Actions and EC2 deployment

The repository has two workflows:

- `.github/workflows/ci.yml` runs on pushes and pull requests. It validates
  Python syntax, JSON/JSONL content, required files, and pinned dependencies.
- `.github/workflows/deploy-ec2.yml` runs after the CI gate for pushes to
  `main`, or manually through **Actions → Deploy to EC2 → Run workflow**.

The deployment workflow creates a release archive from the committed tree,
uploads it over SSH, and sends `deploy/scripts/deploy-release.sh` to the server.
The server keeps immutable releases under `/opt/gdsa-practice/releases/` and
switches `/opt/gdsa-practice/current` atomically. The learner database,
question database, RAG index, virtual environment, and environment file remain
outside release directories.

## Required GitHub configuration

Add these as repository or `production` environment secrets:

| Secret | Value |
|---|---|
| `EC2_HOST` | EC2 DNS name or IP address |
| `EC2_USER` | SSH deployment user |
| `EC2_SSH_PRIVATE_KEY` | Private key matching the EC2 user’s `authorized_keys` |
| `EC2_KNOWN_HOSTS` | Verified `known_hosts` entry for the EC2 host |
| `EC2_SSH_PORT` | Optional SSH port; defaults to `22` |

The deployment contract uses `/opt/gdsa-practice`, matching the checked-in
systemd and Nginx templates. If the EC2 instance uses another root, update
those templates and the deployment script together before enabling CI/CD.

Generate `EC2_KNOWN_HOSTS` from a trusted administrative machine and verify the
fingerprint before saving it as a secret. Do not use `StrictHostKeyChecking=no`
or place the private key in the repository.

## Deployment behavior

The remote script refuses to deploy unless the server already has:

- `/etc/gdsa-practice/gdsa-practice.env`;
- `/opt/gdsa-practice/venv/bin/gunicorn`;
- `/opt/gdsa-practice/data/gdsa-practice.sqlite3`; and
- `/opt/gdsa-practice/data/projects_index.jsonl`.

After the release is activated, systemd is restarted and both direct Gunicorn
and Nginx health endpoints are checked. If restart or health verification
fails, the previous `current` symlink is restored when one exists.

The workflow does not import question banks or rebuild the RAG index. Those are
controlled data-release operations and must be provisioned separately.
