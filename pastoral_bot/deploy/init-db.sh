#!/usr/bin/env bash
set -euo pipefail
# psql quoted variable substitution, never interpolate a password into SQL.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 --set=app_password="$PASTORAL_DATABASE_PASSWORD" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE ROLE pastoral LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER DATABASE pastoral OWNER TO pastoral;
GRANT ALL ON SCHEMA public TO pastoral;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
SQL
