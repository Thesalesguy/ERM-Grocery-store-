FROM python:3.11-slim

WORKDIR /app

# psycopg[binary] ships its own libpq, so no extra system packages are
# required for the database driver.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# M18: without this, `depends_on: condition: service_healthy` (used by
# nginx in docker-compose.prod.yml) has nothing to wait on — nginx could
# start proxying before uvicorn is actually accepting connections. Uses
# Python's stdlib (already present in this image) rather than adding
# curl/wget as a new package just for this check.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
