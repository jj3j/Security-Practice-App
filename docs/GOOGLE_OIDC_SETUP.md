# Google OpenID Connect configuration

The application authorizes exact `(issuer, subject)` identities. An email
address is a profile attribute, not an authorization key.

## Google client

Create a Google OAuth web client with the `openid email profile` scopes. Register
the exact callback URL:

```text
https://<public-host>/auth/callback
```

Set these values in `/etc/security-study/gdsa-practice.env` on the EC2 instance:

```text
GDSA_PUBLIC_BASE_URL=https://<public-host>
GDSA_GOOGLE_CLIENT_ID=<client-id>
GDSA_GOOGLE_CLIENT_SECRET=<client-secret>
```

The application requires an HTTPS public origin for browser login. Keep the
client secret only in the protected server environment file or a secret manager.

## First-owner bootstrap

The first sign-in is recorded as pending and is not automatically approved:

1. Sign in once with the maintainer Google account.
2. On the server, list the pending identity:

   ```bash
   sudo -u gdsa-practice /opt/security-study/venv/bin/python \
     /opt/security-study/current/backend/learner_admin.py \
     --database /var/lib/gdsa-practice/learner.sqlite3 pending
   ```

3. Approve the exact issuer and subject as an owner:

   ```bash
   sudo -u gdsa-practice /opt/security-study/venv/bin/python \
     /opt/security-study/current/backend/learner_admin.py \
     --database /var/lib/gdsa-practice/learner.sqlite3 approve \
     --issuer "https://accounts.google.com" \
     --subject "<exact-pending-subject>" \
     --role owner \
     --label "primary maintainer"
   ```

4. Sign in again and approve a second owner for recovery.

Routine learner approvals should use the owner-only **Manage users** page. Do
not auto-approve the first sign-in and do not use email as an allowlist key.
