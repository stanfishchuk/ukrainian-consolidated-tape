# --- SMIDA tape pipeline ---------------------------------------------------
# Python 3.11 on any platform; wheels for pyarrow/selectolax exist for
# linux/arm64 (Apple Silicon) and linux/amd64.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SMIDA_HOME=/data

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY smida_tape ./smida_tape
RUN pip install --no-cache-dir --no-deps .

RUN useradd --create-home --uid 1000 smida \
 && mkdir -p /data/storage /data/output \
 && chown -R smida:smida /data /app
USER smida

VOLUME ["/data/storage", "/data/output"]
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "smida_tape"]
CMD ["status"]
