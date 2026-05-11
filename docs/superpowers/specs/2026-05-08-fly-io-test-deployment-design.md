# Fly.io test deployment — design

**Goal:** ship a private link that one tester can use to exercise the live app from a browser, with no Google OAuth setup, no local install, and minimal monthly cost. Empty database on first boot.

## Architecture

One Fly app, one VM, one 1 GB volume.

- **Web:** uvicorn on internal port `8080`, behind Fly's HTTPS edge (auto TLS).
- **Storage:** SQLite at `/data/uc.db`. The volume survives deploys; the bind path is fixed in `fly.toml`.
- **Lifecycle:** `auto_stop_machines = "stop"` + `auto_start_machines = true` so the VM idles to zero between visits and warms in under a second on the next request.
- **Migrations:** the entrypoint runs `alembic upgrade head` before exec'ing uvicorn — every deploy converges the live DB on the latest schema before serving traffic.

## Files to add

```
Dockerfile                  # python:3.12-slim, uv sync --frozen, /app workdir
fly.toml                    # one machine, http_service on 8080, volume mount, auto-stop
scripts/fly-entrypoint.sh   # alembic upgrade head && exec uvicorn ...
.dockerignore               # exclude .venv, dev.db, tests/, .git, etc.
```

No application code changes. The dev-login form, the prod validator's gate on `APP_ENV`, and the existing alembic setup are all reused as-is.

## Runtime config

Set via `fly secrets set`:

| Secret | Value | Why |
|--------|-------|-----|
| `APP_ENV` | `test` | Mounts `/auth/_dev-login`; skips the prod-only Google-creds validator |
| `SECRET_KEY` | random 32-byte hex | Signs the session cookie |
| `DATABASE_URL` | `sqlite:////data/uc.db` | SQLite on the mounted volume |
| `APP_BASE_URL` | `https://<picked-name>.fly.dev` | Used in OAuth redirects and audit URLs |
| `BOOTSTRAP_ADMIN_EMAIL` | `micull199@gmail.com` | First dev-login matching this auto-promotes to admin |
| `EMAIL_BACKEND` | `console` | PO emails just log; no SMTP wiring on the test deploy |

## Privacy posture

- The Fly subdomain is the only thing standing between the public and the dev-login form.
- Anyone who finds the URL *can* hit `/auth/_dev-login`. The form accepts any email, but `upsert_user_from_userinfo` lands non-bootstrap emails as `pending`, and `require_active_user` blocks pending accounts from every admin route. They see the pending screen and nothing else.
- The admin (you) signs in first, the dev-login matches `BOOTSTRAP_ADMIN_EMAIL`, you get auto-promoted; then `/admin/users` is used to promote the tester after they hit the link.

If URL secrecy ever feels too thin, a 30-line basic-auth middleware at the ASGI level (gated by `APP_ENV=test`) is the bolt-on. Not in this design.

## Cost shape

`shared-cpu-1x` machine with 256 MB RAM + 1 GB volume + auto-stop. Idle cost is the volume only (≈ \$0.15/mo). Active cost rounds to pennies for a single tester clicking around. Fly requires a card on file but the spend stays small unless something bursts traffic.

## What's deliberately *not* in this design

- No Postgres, no managed DB add-on.
- No CI workflow to auto-deploy.
- No staging vs. prod split — this *is* the test deploy.
- No data seeding on first boot. The tester sees an empty app and exercises the onboarding flow.
- No backups beyond the volume itself. Acceptable because this is a test deploy and the data is throwaway.

## Out-of-band steps

The user runs these themselves (they're interactive and tied to a fly.io login):

1. `curl -L https://fly.io/install.sh | sh` — installs `flyctl`.
2. `fly auth signup` — browser flow, requires a card.
3. Pick an app name.

Everything else — `fly launch --no-deploy`, `fly volumes create`, `fly secrets set`, `fly deploy` — happens here.
