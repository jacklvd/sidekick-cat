# Cloud Run webhook service for sidekick-cat. See README.md#architecture.
FROM python:3.13-slim

WORKDIR /app

# Deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Reused business logic + the new server adapter + templates.
COPY scripts/ scripts/
COPY server/ server/
COPY templates/ templates/

# Cloud Run injects $PORT (default 8080); never hardcode it.
ENV PORT=8080
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
