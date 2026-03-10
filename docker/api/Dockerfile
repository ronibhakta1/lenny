FROM python:3.12

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set non-sensitive environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Expose ports (FastAPI, PostgreSQL, MinIO API, MinIO Console)
# XXX These ports should be variables to reflect compose.yml
EXPOSE 8080 5432 9000 9001 3000 15080

COPY ./lenny/ /app/lenny/
COPY ./alembic/ /app/alembic/
COPY ./alembic.ini /app/alembic.ini
RUN chmod +x /app/lenny/app.py

RUN apt-get update && apt-get install -y \
    nginx \
    libpq-dev \
    postgresql-client
RUN adduser --disabled-password --gecos "" nginx

COPY ./docker/nginx/nginx.conf /etc/nginx/nginx.conf
COPY ./docker/nginx/conf.d/lenny.conf /etc/nginx/conf.d/lenny.conf

# Entrypoint: runs migrations, then starts uvicorn + nginx
COPY ./docker/utils/entrypoint.sh /app/docker/utils/entrypoint.sh
RUN chmod +x /app/docker/utils/entrypoint.sh

CMD ["sh", "/app/docker/utils/entrypoint.sh"]
