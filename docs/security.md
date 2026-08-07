# Security and multi-tenancy

The default configuration is deliberately simple for local evaluation:

- `docker compose up --build` starts without credentials.
- services bind to `127.0.0.1`;
- CORS accepts only the configured local UI origins; the unified gateway uses same-origin API requests;
- MinIO objects remain private and browser links are short-lived signed URLs.

Local mode is not a production security boundary. Production startup fails closed
unless API-key authentication is configured.

## Production configuration

Generate a random secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set the deployment environment, enable authentication, and provide one or more
key identities through the platform's secret manager:

```dotenv
APP_ENVIRONMENT=production
AUTH_MODE=api_key
AUTH_API_KEYS_JSON=[{"id":"plant-a-admin","key":"REPLACE_WITH_RANDOM_SECRET","tenant_id":"plant-a","roles":["admin"]}]
CORS_ALLOWED_ORIGINS=https://agent.example.com
MINIO_PUBLIC_READ=false
```

Clients send the secret as `X-API-Key`. Supported roles are:

| Role | Access |
|---|---|
| `query` | query, stream, history, feedback, and query-run recovery |
| `import` | file import, import status, and import-run recovery |
| `workflow` | human-review cases, actions, and workflow delivery operations |
| `admin` | all current API operations |

Never commit `AUTH_API_KEYS_JSON` to Git. Rotate a key by adding a new identity,
deploying it, migrating clients, and then removing the old identity.

Every browser-facing service exposes `GET /auth/me`. The bundled HTML pages call
this endpoint before mounting a module, hide navigation entries outside the
current role set, and show an access-denied state for direct unauthorized URLs.
The application center also displays the current tenant and key identity and
allows the browser credential to be replaced or cleared without exposing it.
This improves least-privilege navigation but is not the final security boundary;
all protected APIs continue to enforce roles on the server.

For a standalone deployment, `AUTH_MODE=password` enables email registration and
login backed by MongoDB. Passwords use salted scrypt hashes. Browser sessions use
an HttpOnly, SameSite cookie, while MongoDB stores only a hash of the random
session token. Public registration is separately controlled and always creates a
least-privilege `query` user in the configured registration tenant:

```dotenv
APP_ENVIRONMENT=production
AUTH_MODE=password
AUTH_REGISTRATION_ENABLED=true
AUTH_REGISTRATION_TENANT_ID=public
AUTH_EMAIL_VERIFICATION_REQUIRED=true
AUTH_EMAIL_VERIFICATION_TTL_SECONDS=1800
AUTH_PUBLIC_BASE_URL=https://agent.example.com
AUTH_SESSION_TTL_SECONDS=604800
AUTH_COOKIE_SECURE=true
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=REPLACE_WITH_SMTP_USER
SMTP_PASSWORD=REPLACE_WITH_SMTP_SECRET
SMTP_FROM_ADDRESS=noreply@example.com
SMTP_SECURITY=starttls
CORS_ALLOWED_ORIGINS=https://agent.example.com
```

Production startup rejects public registration unless email verification is
enabled. Verification tokens are random, single-use, expire automatically, and
are stored in MongoDB only as SHA-256 hashes. Requesting a new link invalidates
the previous link. SMTP delivery runs behind the provider-neutral
`VerificationEmailSender` interface, so a hosted email API can replace the
standard SMTP adapter without changing registration or verification routes.
Never commit `SMTP_PASSWORD`; supply it through the deployment secret manager.

GitHub OAuth is an optional login provider in password mode. Configure the
GitHub OAuth App callback as
`https://agent.example.com/auth/oauth/github/callback`, then enable it with:

```dotenv
AUTH_GITHUB_OAUTH_ENABLED=true
AUTH_GITHUB_CLIENT_ID=REPLACE_WITH_GITHUB_CLIENT_ID
AUTH_GITHUB_CLIENT_SECRET=REPLACE_WITH_GITHUB_CLIENT_SECRET
```

The callback requires a short-lived HttpOnly state cookie, accepts only verified
GitHub email addresses, and creates first-time users with the `query` role. A
matching existing email is linked to that user without changing its tenant or
roles. OAuth identities are stored separately with unique provider/subject and
provider/user constraints. Production OAuth callbacks require HTTPS. Never
commit the client secret.

API keys remain available in password mode for service-to-service automation.
An enterprise deployment can later replace password authentication with its
OIDC or identity-aware gateway while preserving the server-controlled
`Principal` contract. Infrastructure consoles and Swagger must also be protected
at the gateway or kept on private networks; hiding their links is insufficient.

Email verification proves control of the submitted inbox, but it does not stop
automated registrations. Public deployments should still add gateway-level bot
protection and abuse monitoring.

## Tenant boundaries

The authenticated `tenant_id` is server-controlled and is applied to:

- Agent run records and recovery lookups;
- internal MongoDB conversation session keys;
- local upload directories;
- MinIO PDF and image object prefixes;
- Milvus item-name and chunk inserts, idempotent deletes, and query filters;
- LangGraph and observability metadata.

Run IDs remain globally unique, while every public lookup also checks the caller's
tenant. A caller receives `404` for another tenant's run instead of learning that
it exists.

Existing MongoDB history and Milvus entities created before this feature do not
contain tenant metadata. Back them up, assign an owning tenant, and migrate or
re-import them before enabling production authentication. Tenant-filtered
retrieval intentionally does not fall back to unscoped records.

## Storage and request hardening

- MinIO anonymous bucket access is disabled by default.
- Stable `minio://` object references are stored in knowledge chunks and history;
  browser responses resolve them to short-lived signed URLs.
- Upload names are reduced to a basename, restricted to `.pdf` and `.md` by
  default, and capped at 100 MiB.
- CORS never accepts `*`; cookie credentials are disabled.
- Responses include request IDs and baseline browser security headers.
- API keys are compared in constant time and are never returned or logged.
- Structured logs must not contain passwords, API keys, cookies, complete user
  questions or answers, document bodies, or other confidential payloads.
- `LOG_DIAGNOSE` must remain disabled in production because exception diagnostics
  can otherwise expose local variables. Loki and Grafana must stay behind the
  internal gateway or private network.

Relevant settings:

| Setting | Default |
|---|---|
| `MAX_UPLOAD_BYTES` | `104857600` |
| `ALLOWED_UPLOAD_EXTENSIONS` | `.pdf,.md` |
| `MINIO_PRESIGNED_URL_TTL_SECONDS` | `3600` |
| `CORS_ALLOWED_ORIGINS` | local import and query origins |

## Deployment checklist

1. Terminate TLS at the ingress or load balancer.
2. Keep MongoDB, Milvus, MinIO, and etcd on private networks.
3. Replace all example database and object-store credentials.
4. Store API and model credentials in a managed secret store.
5. Configure per-tenant keys with least-privilege roles.
6. Restrict CORS to the actual frontend origins.
7. Back up MongoDB, Milvus, and MinIO and test restoration.
8. Forward audit logs and `X-Request-ID` values to the central log platform.
9. Add gateway-level rate limits, WAF policy, and key-revocation automation.
10. Run the deterministic evaluation and an authorized tenant-isolation test
    before each release.
