#!/bin/sh
# Init PostgreSQL
if [ ! -s /var/lib/postgresql/data/PG_VERSION ]; then
  echo "Initializing PostgreSQL database..."
  mkdir -p /var/lib/postgresql/data /run/postgresql
  chown -R postgres:postgres /var/lib/postgresql/data /run/postgresql
  chmod 700 /var/lib/postgresql/data /run/postgresql
  
  su - postgres -c "initdb -D /var/lib/postgresql/data"
  
  # Update PostgreSQL configuration
  echo "host all all 0.0.0.0/0 md5" >> /var/lib/postgresql/data/pg_hba.conf
  echo "listen_addresses = '*'" >> /var/lib/postgresql/data/postgresql.conf
fi

# Start PostgreSQL
echo "Starting PostgreSQL..."
mkdir -p /run/postgresql
chown -R postgres:postgres /run/postgresql
su - postgres -c "pg_ctl -D /var/lib/postgresql/data -l /tmp/pg.log start"

# Wait for PostgreSQL to start
echo "Waiting for PostgreSQL to start..."
sleep 5

# Check if PostgreSQL is running
echo "Checking PostgreSQL status..."
su - postgres -c "pg_ctl -D /var/lib/postgresql/data status" || echo "PostgreSQL failed to start - check /tmp/pg.log"

# Skipped automatic DB/user creation - will be handled manually later if needed
echo "Skipping PostgreSQL user/database setup"

# Setting default MinIO credentials if not provided ( will be used for testing ) changed later in production
if [ -z "$MINIO_ROOT_USER" ]; then
  export MINIO_ROOT_USER=dev_user
fi
if [ -z "$MINIO_ROOT_PASSWORD" ]; then
  export MINIO_ROOT_PASSWORD=dev_password
fi

# Start MinIO
echo "Starting MinIO..."
minio server /data/minio --address ":9000" --console-address ":9001" &

# Start the direct uvicorn server
echo "Starting FastAPI with uvicorn..."
export PYTHONPATH=/app
cd /app
python -m uvicorn lenny.app:app --host 0.0.0.0 --port 7002 --log-level debug &
sleep 3

# Start NGINX
echo "Starting NGINX..."
nginx -t && nginx &
sleep 3

# Print debugging information
# echo "Checking NGINX configuration:"
# nginx -t

# echo "Network interfaces:"
# ip addr

echo "Listening ports:"
netstat -tulpn || echo "netstat not available"

# echo "Running processes:"
# ps aux | grep -E 'nginx|python|postgres'

echo "NGINX error log:"
cat /var/log/nginx/error.log || echo "No NGINX error log found"

echo "Services running:"
ps aux | grep -E "nginx|python|postgres"

wait