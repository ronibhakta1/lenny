#!/usr/bin/env bash

ENV_FILE=".env"

# Exit if the file already exists
if [ -f "$ENV_FILE" ]; then
  echo "$ENV_FILE already exists. No changes made."
  exit 0
fi

genpass() {
    len=${1:-32}
    dd if=/dev/urandom bs=1 count=$((len * 2)) 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | head -c "$len"
}

# Use environment variables if they are set, otherwise provide defaults or generate secure values
LENNY_HOST="localhost"
LENNY_PORT="${LENNY_PORT:-8080}"
LENNY_WORKERS="${LENNY_WORKERS:-1}"
LENNY_LOG_LEVEL="${LENNY_LOG_LEVEL:-debug}"
LENNY_RELOAD="${LENNY_RELOAD:-1}"
LENNY_SSL_CRT="${LENNY_SSL_CRT:-}"
LENNY_SSL_KEY="${LENNY_SSL_KEY:-}"

READER_PORT="${READER_PORT:-3000}"
READIUM_PORT="${READIUM_PORT:-15080}"

DB_USER="${POSTGRES_USER:-librarian}"
DB_HOST="${POSTGRES_HOST:-127.0.0.1}"
DB_PORT="${POSTGRES_PORT:-5432}"

DB_PASSWORD="${POSTGRES_PASSWORD:-$(genpass 32)}"
DB_NAME="${DB_NAME:-lenny}"

S3_ACCESS_KEY="${MINIO_ROOT_USER:-$(genpass 20)}"
S3_SECRET_KEY="${MINIO_ROOT_PASSWORD:-$(genpass 40)}"
S3_ENDPOINT="${S3_ENDPOINT:-http://s3:9000}"

# Write to lenny.env
cat <<EOF > "$ENV_FILE"
# API
LENNY_PROXY=
LENNY_HOST=$LENNY_HOST
LENNY_PORT=$LENNY_PORT
LENNY_WORKERS=$LENNY_WORKERS
LENNY_LOG_LEVEL=$LENNY_LOG_LEVEL
LENNY_RELOAD=$LENNY_RELOAD
LENNY_SSL_CRT=$LENNY_SSL_CRT
LENNY_SSL_KEY=$LENNY_SSL_KEY

# Service Ports
READER_PORT=$READER_PORT
READIUM_PORT=$READIUM_PORT

# DB
DB_USER=$DB_USER
DB_HOST=$DB_HOST
DB_PORT=$DB_PORT
DB_PASSWORD=$DB_PASSWORD
DB_NAME=$DB_NAME
DB_TYPE=postgres

# S3 Credentials
S3_ACCESS_KEY=$S3_ACCESS_KEY
S3_SECRET_KEY=$S3_SECRET_KEY
S3_ENDPOINT=$S3_ENDPOINT
S3_PROVIDER=minio
S3_SECURE=false

EOF
