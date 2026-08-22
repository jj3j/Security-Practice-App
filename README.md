# Security Study Application

This folder is a standalone deployment repository for the hosted application.
The authoring workspace under `D:\Study` is intentionally outside this
repository. Runtime databases, RAG indexes, credentials, and learner state are
also kept outside Git.

The hosted application is split into two independent components:

- `frontend/`: static HTML, CSS, and JavaScript served by Nginx.
- `backend/`: a WSGI API served by Gunicorn with a read-only SQLite runtime.
- `content/`: locally generated study guides and flashcards, excluded from Git;
  production provisions them independently under
  `/opt/security-study/data/content`.

The local authoring sources remain:

```text
Study Projects/SEC530 - GDSA/sample_questions/questions.jsonl
Study Projects/CISSP/Sample Questions/questions.jsonl
Study Projects/GIAC GMON/sample_questions/Questions
Study Projects/GIAC GMON/sample_questions/Answers
```

The application includes the `GIAC GMON` Study catalog, dedicated retrieval
evidence, and a committed release question bank:

```text
practice_app/content/gmon-study.json
practice_app/content/gmon-study-sources.jsonl
practice_app/question_banks/gmon-questions.jsonl
```

GMON provides four Practice bundles and four Exam bundles of 82 questions each.
Its five Study units map to SEC511 Books 511.1-511.5. The release bank is a
deterministic build artifact from the four validated authoring exams; edit the
authoring source and regenerate it instead of hand-editing the JSONL.

Learner identity, progress, flashcard state, and exam attempts use a separate
writable SQLite database. Production stores it at:

```text
/var/lib/gdsa-practice/learner.sqlite3
```

The existing question database remains immutable and read-only at runtime.
The project-aware JSONL index is also read-only and supplies chapter-scoped
grounded evidence. Only identity and progress records are written to the
learner database.
Initialize learner state and bootstrap the first exact OpenID Connect owner with:

```bash
python learner_admin.py \
  --database /var/lib/gdsa-practice/learner.sqlite3 \
  init

python learner_admin.py \
  --database /var/lib/gdsa-practice/learner.sqlite3 \
  approve \
  --issuer "https://issuer.example" \
  --subject "exact-oidc-subject" \
  --role owner \
  --label "initial owner"
```

Authorization uses the exact `(issuer, subject)` pair. Email addresses are
profile attributes only and are never used as allowlist keys. After an owner is
bootstrapped, routine learner approvals use the owner-only **Manage users** page.
Keep `learner_admin.py` only for first-owner bootstrap and emergency recovery.

## Google OpenID Connect owner bootstrap

For the complete Google Cloud, Ubuntu, two-owner, verification, recovery, and
troubleshooting procedure, see
[`docs/GOOGLE_OIDC_SETUP.md`](docs/GOOGLE_OIDC_SETUP.md).

Configure a Google Cloud OAuth web client with an External audience, the
`openid email profile` scopes, and this exact redirect URI:

```text
https://<public-host>/auth/callback
```

Set the Google client ID, client secret, and public HTTPS origin in the
protected production environment file. Do not commit credentials.

The maintainer bootstrap deliberately does not auto-approve the first person to
sign in. Use this lockout-safe process instead:

1. Sign in once with the maintainer Google account. The app validates Google,
   records the exact identity as pending, and denies access.
2. On the server, list pending identities:

   ```bash
   python learner_admin.py \
     --database /var/lib/gdsa-practice/learner.sqlite3 \
     pending
   ```

3. Confirm the displayed email/name, then approve the exact issuer and subject
   as the owner:

   ```bash
   python learner_admin.py \
     --database /var/lib/gdsa-practice/learner.sqlite3 \
     approve \
     --issuer "https://accounts.google.com" \
     --subject "<exact-pending-subject>" \
     --role owner \
     --label "primary maintainer"
   ```

4. Sign in again. Approve a second Google identity as another owner for account
   recovery. The CLI refuses to disable or demote the last enabled owner.

## Approving learners from the web application

1. Ask the learner to sign in with Google once. The validated exact identity is
   recorded as pending, but access remains denied.
2. Sign in as an owner and select **Manage users**.
3. Verify the displayed issuer, subject, email, and name, then select
   **Approve as learner**.
4. Ask the learner to sign in again.

The backend rechecks the current owner role and CSRF token for every approval.
Approvals are atomic and audit logged without using email as an authorization
key. The page cannot create an identity that has not completed a validated
Google sign-in.

Create a runtime database:

```powershell
& "C:\Users\Surya\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -B `
  practice_app\backend\import_questions.py `
  --source "Study Projects\SEC530 - GDSA\sample_questions\questions.jsonl" `
  --database "<destination>\gdsa-practice.sqlite3" `
  --project "SEC530 - GDSA" `
  --skip-invalid
```

Create a multi-course runtime database for SEC530, CISSP, and GMON:

```powershell
& "C:\Users\Surya\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -B `
  practice_app\backend\import_questions.py `
  --database "<destination>\gdsa-practice.sqlite3" `
  --bank "SEC530 - GDSA=Study Projects\SEC530 - GDSA\sample_questions\questions.jsonl" `
  --bank "CISSP=Study Projects\CISSP\Sample Questions\questions.jsonl" `
  --bank "GIAC GMON=practice_app\question_banks\gmon-questions.jsonl" `
  --skip-invalid
```

The importer defaults to strict validation and performs atomic replacement.
`--skip-invalid` must be explicit and reports every excluded record.

Production startup uses:

```bash
gunicorn --config gunicorn.conf.py
```

Required environment variables are documented in
`deploy/environment/gdsa-practice.env.example`. See
[`docs/EC2_DEPLOYMENT.md`](docs/EC2_DEPLOYMENT.md) for the complete
EC2/Nginx/systemd deployment and recovery runbook.

## Local checks

Run the same dependency-free repository validation used by GitHub Actions:

```powershell
python scripts\validate_repo.py
python -m unittest discover -s tests -v
python -m compileall backend scripts
```

## GitHub Actions and EC2

CI runs for pushes and pull requests. A push to `main` runs CI first, then
uploads an immutable release archive over SSH and activates it on EC2 with an
atomic `current` symlink. Configure the required secrets and one-time server
prerequisites in [`docs/GITHUB_ACTIONS.md`](docs/GITHUB_ACTIONS.md) and
[`docs/EC2_DEPLOYMENT.md`](docs/EC2_DEPLOYMENT.md).
