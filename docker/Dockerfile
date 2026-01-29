# ─────────────────────────────────────────────
# Base image
# ─────────────────────────────────────────────
FROM python:3.12-slim

# Prevent Python from writing pyc files & buffering
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# System dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy project files
COPY . .

# Collect static files (safe even if empty)
RUN python manage.py collectstatic --noinput || true

# Expose Django port
EXPOSE 8000

# Default command (overridden by docker-compose)
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
