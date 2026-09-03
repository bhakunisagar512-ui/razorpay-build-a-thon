#!/bin/bash
# Creates the extra databases Temporal needs, alongside the app DB.
# Runs once, on first init of the pgdata volume.
set -euo pipefail

for db in $(echo "${POSTGRES_EXTRA_DBS:-}" | tr ',' ' '); do
  echo "creating database: $db"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE $db'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db')\gexec
EOSQL
done
