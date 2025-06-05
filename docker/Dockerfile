FROM python:3.11

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set non-sensitive environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Expose ports (FastAPI, PostgreSQL, MinIO API, MinIO Console)
EXPOSE 8080 5432 9000 9001

COPY ./lenny/ /app/lenny/
RUN chmod +x /app/lenny/app.py

COPY ./scripts/ /app/scripts/

RUN apt-get update && apt-get install -y \
    nginx \
    libpq-dev \
    postgresql-client
RUN adduser --disabled-password --gecos "" nginx

COPY ./docker/nginx/nginx.conf /etc/nginx/nginx.conf
COPY ./docker/nginx/conf.d/lenny.conf /etc/nginx/conf.d/lenny.conf
COPY ./docker/entrypoint.sh /app/docker/entrypoint.sh
RUN chmod +x /app/docker/entrypoint.sh

# Run FastAPI app, Nginx, and book loader script
CMD ["sh", "/app/docker/entrypoint.sh"]
