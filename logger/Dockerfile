FROM python:3.12-slim

# Print logs straight away; no .pyc files in the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FEEDLOGGER_CONFIG=/config/config.toml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY feedlogger/ feedlogger/

# Run as an ordinary user, not root. UID 1000 is the first user on most Linux
# VMs, so files the logger writes in ./data stay owned by you on the host.
ARG UID=1000
RUN useradd --uid "${UID}" --no-create-home --shell /usr/sbin/nologin logger
USER logger

ENTRYPOINT ["python", "-m", "feedlogger"]
CMD ["run"]
