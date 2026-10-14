# Pinned by digest (E.2 supply-chain hardening): python:3.11-slim,
# multi-arch index digest as of 2026-09-23 (resolves to 3.11.16-slim-trixie).
# A digest pin makes the build deterministic and immune to tag mutations
# upstream; bump it deliberately with a re-audit.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Non-root user (4.1): the container must not run as root.
RUN useradd --create-home --uid 10001 appuser

# Install dependencies first (layer caching).
COPY pyproject.toml README.md ./
COPY finance_agent ./finance_agent
COPY model_bench ./model_bench
COPY generate_data.py config.yaml ./
RUN pip install .

# App sources + the custom Streamlit theme (it was silently lost before).
COPY app ./app
COPY .streamlit ./.streamlit

# Shared bootstrap entrypoint (data + model when missing) used by both the
# app and the API services. `exec "$@"` keeps signals forwarded to the child.
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

RUN mkdir -p /app/data /app/model_bench/results /app/reports && \
    chown -R 10001:10001 /app

USER appuser

EXPOSE 8501 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"]

# Bootstrap data + model ONLY when the artifacts are missing (named volumes
# persist them across restarts, so `docker compose restart` does not retrain).
# A failing bootstrap exits the container (bounded by compose `on-failure:5`),
# instead of an unbounded `unless-stopped` crash loop.
STOPSIGNAL SIGTERM
CMD ["/app/docker-entrypoint.sh", "streamlit", "run", "app/Home.py", "--server.port", "8501", "--server.address", "0.0.0.0"]
