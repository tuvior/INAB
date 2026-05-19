FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INAB_DATA_DIR=/data

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY src ./src

RUN uv pip install --system .

EXPOSE 8000
VOLUME ["/data"]

CMD ["uvicorn", "inab.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
