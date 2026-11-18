FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FLOOD_DB_PATH=/data/flood.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# uvicorn workers > 1 is safe: SQLite WAL + BEGIN IMMEDIATE serialise
# cross-process writers, and every revision commit is atomic.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
