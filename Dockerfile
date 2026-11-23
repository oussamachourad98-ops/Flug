FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY flug ./flug
RUN pip install ".[postgres]" \
 && useradd --create-home --uid 10001 flug \
 && mkdir -p /app/data && chown flug:flug /app/data

USER flug
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["flug", "serve"]
