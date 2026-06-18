# ─────────────────────────────────────────────
# Base image
# ─────────────────────────────────────────────
FROM python:3.12-slim

# Prevent Python from writing pyc files & buffering
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ── System dependencies ───────────────────────────────────────────────────────
# NOTE: We install 'docker-cli' (the client binary only, ~7 MB) rather than
# 'docker.io' (the full daemon, which pulls gcc/systemd/containerd adding
# ~930 MB and breaking incremental BuildKit layer caching on every build).
#
# The Django app only needs the docker *client* to run:
#   docker exec netily_radius ...   (RADIUS reload)
#   docker restart netily_radius    (signal from network signals.py)
#   docker exec netily_haproxy ...  (HAProxy reload)
#
# WireGuard manager talks to the Docker socket directly (no CLI needed).
# The docker-compose CLI is not called from within the container.
# ─────────────────────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    netcat-openbsd \
    iproute2 \
    docker-cli \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /app

# Install Python dependencies first (layer caching)
# Copy the entire requirements folder
COPY requirements/ /app/requirements/

# Install dependencies from production.txt
RUN pip install --upgrade pip && \
    pip install -r /app/requirements/production.txt

# Copy project files
COPY . .

# Collect static files (safe even if empty)
RUN python manage.py collectstatic --noinput || true

# Expose Django port
EXPOSE 8000

# Default command (overridden by docker-compose)
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]