FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-core.txt requirements-operational.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -r requirements-operational.txt

COPY app ./app
COPY models ./models
COPY reports ./reports
# The snapshot may not exist yet. Naming a file that always does keeps this COPY from failing.
COPY requirements-core.txt demo_snapshot* ./demo_snapshot/

RUN useradd --create-home --uid 1000 sentinel \
    && mkdir -p instance storage logs \
    && chown -R sentinel:sentinel instance storage logs
USER sentinel

ENV DATABASE_URL=sqlite:///./instance/sentinelwatch.db \
    STORAGE_DIR=storage \
    LOG_DIR=logs \
    SENTINELWATCH_ML_DEVICE=cpu

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "5"]
