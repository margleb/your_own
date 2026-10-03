# syntax=docker/dockerfile:1
FROM postgres:16-bookworm AS pg-client

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/var/lib/pastoral-bot/huggingface \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libpq5 liblz4-1 libzstd1 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 pastoral \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/pastoral-bot pastoral
COPY --from=pg-client /usr/lib/postgresql/16/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=pg-client /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/pg_restore
WORKDIR /app
# Install CPU wheels explicitly: the ordinary PyPI torch build brings CUDA.
RUN python -m pip install --no-cache-dir torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu
COPY pastoral_bot/requirements.txt /tmp/pastoral-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/pastoral-requirements.txt \
    && pg_dump --version && pg_restore --version
COPY pastoral_bot /app/pastoral_bot
COPY infrastructure/__init__.py infrastructure/paths.py /app/infrastructure/
COPY infrastructure/llm/__init__.py infrastructure/llm/client.py infrastructure/llm/call_log.py /app/infrastructure/llm/
COPY infrastructure/telegram/__init__.py infrastructure/telegram/client.py /app/infrastructure/telegram/
COPY infrastructure/logging /app/infrastructure/logging/
RUN mkdir -p /var/lib/pastoral-bot && chown 10001:10001 /var/lib/pastoral-bot
USER 10001:10001
ENTRYPOINT ["python", "-m", "pastoral_bot"]
CMD ["run"]
