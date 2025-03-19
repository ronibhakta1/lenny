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

# Create DB/user if they don't exist
echo "Setting up PostgreSQL user and database..."
su - postgres -c "psql -c \"CREATE USER $POSTGRES_USER WITH PASSWORD '$POSTGRES_PASSWORD';\"" || true
su - postgres -c "psql -c \"CREATE DATABASE $POSTGRES_DB OWNER $POSTGRES_USER;\"" || true
su - postgres -c "psql -c \"ALTER USER $POSTGRES_USER WITH SUPERUSER;\"" || true

# Start MinIO
echo "Starting MinIO..."
minio server /data/minio --address ":9000" --console-address ":9001" &

# Start NGINX
echo "Starting NGINX..."
nginx &

# Set up socket directory permissions
echo "Setting up socket directory permissions..."
mkdir -p /tmp
chmod 1777 /tmp

# Start UWSGI
echo "Starting uWSGI..."
uwsgi --ini /app/uwsgi.ini &

# Wait a moment for the socket to be created
sleep 2

# Make sure NGINX has permission to access the socket
echo "Fixing socket permissions..."
chmod 777 /tmp/uwsgi.sock
chown nginx:www-data /tmp/uwsgi.sock || echo "Failed to set socket ownership"


echo "All services started!"
# First test with a basic WSGI app to ensure connections work
echo "Testing with basic WSGI app..."
uwsgi --socket /tmp/uwsgi_test.sock --chmod-socket=777 --file /app/test_uwsgi.py --callable=application --master --processes 1 --uid nginx --gid www-data --logto /tmp/uwsgi_test.log &
sleep 2
# Fix this line
echo "Testing socket connection..."
curl --unix-socket /tmp/uwsgi_test.sock http://localhost/ || echo "Socket test failed"

# Then start the actual application
# echo "Starting main uWSGI application..."
# uwsgi --ini /app/uwsgi.ini &

# Debug info
echo "Socket files:"
ls -la /tmp/*.sock

# Add this near the end
# If uWSGI is having issues, try running with uvicorn directly 
echo "Testing direct FastAPI with uvicorn..."
python /app/test.py &

# Add debugging output before the final wait
echo "NGINX error log:"
cat /var/log/nginx/error.log || echo "No NGINX error log found"

echo "uWSGI log:"
cat /tmp/uwsgi.log || echo "No uWSGI log found"

wait