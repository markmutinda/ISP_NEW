# ─────────────────────────────────────────────
# Railway Production Dockerfile
# ─────────────────────────────────────────────
FROM python:3.12-slim

# Prevent Python from writing pyc files & buffering
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Django settings module for production
ENV DJANGO_SETTINGS_MODULE=config.settings.production

# System dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements/ /app/requirements/
RUN pip install --upgrade pip && \
    pip install -r /app/requirements/production.txt

# Copy project files
COPY . .

# Create static directory and collect static files
RUN mkdir -p /app/staticfiles && \
    python manage.py collectstatic --noinput || true

# Expose Django port
EXPOSE 8000

# Start with gunicorn - Railway sets PORT env var
CMD gunicorn config.wsgi:application \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers 3 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
