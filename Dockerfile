FROM python:3.11-slim

# uv binary, pinned version, straight from its distroless image (no pip/curl needed)
COPY --from=ghcr.io/astral-sh/uv:0.5.29 /uv /uvx /usr/local/bin/

# gosu drops from root to the runtime PUID/PGID before exec'ing the app (see entrypoint.sh),
# so files the daemon writes on NAS bind mounts are owned by a real user, not root.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies only (--no-install-project skips building/installing this package itself,
# which needs README.md — irrelevant here since we run `python main.py` directly, not the
# console-script entry point).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY aws_copier/ ./aws_copier/
COPY main.py ./
COPY find_duplicates.py ./
COPY find_scattered_duplicates.py ./
COPY reorganize_cellphone_backup.py ./
COPY cleanup_migrated_s3.py ./

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "main.py"]
