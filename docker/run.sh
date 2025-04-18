#!/bin/bash

# Wait for the PostgreSQL service to be ready
echo "Waiting for PostgreSQL..."
until pg_isready -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB"; do
  sleep 2
done
echo "PostgreSQL is ready"

# Check if items table exists
TABLE_EXISTS=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'items');" | grep -o 't')

# Check if versions directory is empty inside Docker
VERSIONS_COUNT=$(ls -A /app/lenny/migrations/versions/ 2>/dev/null | wc -l || echo 0)

# Mount point for local migrations folder (assumes ./lenny/migrations/versions is mounted)
LOCAL_VERSIONS="/app/lenny/migrations/versions"

# If versions folder is empty or items table is missing, generate initial migration and copy to local
if [ "$VERSIONS_COUNT" -eq 0 ] || [ "$TABLE_EXISTS" != "t" ]; then
  echo "Initializing database with migrations..."
  # Drop alembic_version table if it exists (only when initializing)
  PGPASSWORD="$POSTGRES_PASSWORD" psql -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP TABLE IF EXISTS alembic_version;"
  # Stamp base to initialize revision history
  alembic -c /app/alembic.ini stamp base
  # Generate initial migration
  alembic -c /app/alembic.ini revision -m "initial migration" --autogenerate || {
    echo "Autogeneration failed, creating manual migration";
    alembic -c /app/alembic.ini revision -m "manual initial migration";
    echo "Manually edit $LOCAL_VERSIONS/latest_migration.py to include table definitions.";
    exit 1;
  }
  # Copy the generated migration to the local folder
  NEW_MIGRATION=$(ls -t /app/lenny/migrations/versions/ | head -n 1)
  if [ -n "$NEW_MIGRATION" ]; then
    cp "/app/lenny/migrations/versions/$NEW_MIGRATION" "$LOCAL_VERSIONS/"
  fi
  # Apply the migration
  alembic -c /app/alembic.ini upgrade head || { echo "Migration upgrade failed"; exit 1; }
else
  # Get current revision and head revision
  CURRENT_REVISION=$(alembic -c /app/alembic.ini current 2>/dev/null | awk '{print $1}' || echo "none")
  HEAD_REVISION=$(alembic -c /app/alembic.ini heads 2>/dev/null | head -n 1 | awk '{print $1}')

  # Check for schema changes using autogenerate, avoid generating if no changes
  CHECK_OUTPUT=$(alembic -c /app/alembic.ini check --autogenerate 2>&1)
  if [ "$CURRENT_REVISION" != "$HEAD_REVISION" ] || echo "$CHECK_OUTPUT" | grep -q "no changes"; then
    if ! echo "$CHECK_OUTPUT" | grep -q "no changes"; then
      echo "Generating schema update migration..."
      alembic -c /app/alembic.ini revision -m "auto-update" --autogenerate || { echo "Autogeneration failed"; exit 1; }
      # Copy the new migration to the local folder
      NEW_MIGRATION=$(ls -t /app/lenny/migrations/versions/ | head -n 1)
      if [ -n "$NEW_MIGRATION" ] && [ ! -f "$LOCAL_VERSIONS/$NEW_MIGRATION" ]; then
        cp "/app/lenny/migrations/versions/$NEW_MIGRATION" "$LOCAL_VERSIONS/"
      fi
      # Apply the new migration
      alembic -c /app/alembic.ini upgrade head || { echo "Migration upgrade failed"; exit 1; }
    fi
  fi
fi

# Apply all migrations (ensure latest state)
echo "Applying migrations..."
alembic -c /app/alembic.ini upgrade head || { echo "Migration upgrade failed"; exit 1; }

# Start FastAPI and NGINX
echo "Starting application..."
python -m uvicorn lenny.app:app --host 0.0.0.0 --port 1337 --workers="${LENNY_WORKERS:-1}" --log-level="${LENNY_LOG_LEVEL:-info}" & exec nginx