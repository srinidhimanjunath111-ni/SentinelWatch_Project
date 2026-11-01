FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements-core.txt requirements-operational.txt ./
RUN pip install --no-cache-dir -r requirements-operational.txt
COPY . .
RUN mkdir -p instance storage/uploads storage/outputs

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

