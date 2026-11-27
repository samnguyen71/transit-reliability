FROM python:3.12-slim

# Print logs straight away; no .pyc files in the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY transit_api/ transit_api/

# Run as an ordinary user, not root. UID 1000 owns ./data on the VM (the logger and the
# nightly job write it), and the API only reads it.
ARG UID=1000
RUN useradd --uid "${UID}" --no-create-home --shell /usr/sbin/nologin api
USER api

EXPOSE 8000
# One worker: the rollup cache, the parsed live snapshot and the Postgres pool are per
# process, and a second worker would double them on an 887 MiB VM.
# 0.0.0.0 is the container's own interface, which Caddy reaches over Compose's network.
# The host side is published on 127.0.0.1 only (docker-compose.yml).
CMD ["uvicorn", "transit_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
