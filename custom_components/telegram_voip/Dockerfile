FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/data/cache \
    TGVOIP_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates espeak-ng ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/app/
COPY rootfs/ /

RUN chmod a+x /usr/bin/telegram_voip && python3 -c "import app.main"

EXPOSE 3332
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python3 -c "import socket; socket.create_connection(('127.0.0.1', 3332), 5).close()" || exit 1

CMD ["/usr/bin/telegram_voip"]
