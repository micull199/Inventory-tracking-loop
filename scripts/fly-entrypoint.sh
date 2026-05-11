#!/usr/bin/env sh
# Fly.io entrypoint: converge the schema on /data/uc.db before serving traffic.
# Runs on the production machine (not a release_command VM) so the mounted
# volume is visible. Migrations are idempotent — alembic upgrade head is a
# no-op when the DB is already current.
set -e

alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
