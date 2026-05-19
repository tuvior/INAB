FROM python:3.14-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INAB_DATA_DIR=/data \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apk add --no-cache ca-certificates \
    && pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./

RUN uv export --frozen --no-dev --no-emit-project --no-hashes --output-file /tmp/requirements.txt > /dev/null \
    && uv pip install --system --no-cache --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY src ./src

EXPOSE 8000
VOLUME ["/data"]

CMD ["python", "-m", "uvicorn", "inab.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
