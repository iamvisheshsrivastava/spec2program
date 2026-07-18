# ============================================================================
# spec2program - Production Docker image
# ----------------------------------------------------------------------------
# Single-stage, slim image. The backend also serves the static frontend,
# so one container runs the whole app.
# ============================================================================
FROM python:3.11-slim

# Do not write .pyc files and force unbuffered stdout/stderr (better logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY data/ ./data/

# The container listens on 8000. Hosting platforms (Render, Railway, Fly.io,
# Heroku, Azure) usually inject a $PORT env var, which we honor at runtime.
EXPOSE 8000

# Use shell form so ${PORT:-8000} expands. Defaults to 8000 locally.
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
