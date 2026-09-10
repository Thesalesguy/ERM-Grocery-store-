FROM python:3.11-slim

WORKDIR /app

# psycopg[binary] ships its own libpq, so no extra system packages are
# required for the database driver.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
