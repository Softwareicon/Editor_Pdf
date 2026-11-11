#!/bin/sh
# Start script for Render. Render provides DATABASE_URL as postgres:// or postgresql://;
# SQLAlchemy needs the psycopg driver name, so convert it before starting.
set -e
if [ -n "$DATABASE_URL" ]; then
  PDFMASTER_DATABASE_URL="$(printf '%s' "$DATABASE_URL" | sed -E 's#^postgres(ql)?://#postgresql+psycopg://#')"
  export PDFMASTER_DATABASE_URL
fi
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers
