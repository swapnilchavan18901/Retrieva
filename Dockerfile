FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Prisma's Python client invokes its generated query engine at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Generate the Prisma client at image build time. Values are build-only placeholders;
# real database credentials are injected by Compose/server runtime.
RUN DATABASE_URL='postgresql://build:build@localhost:5432/build' \
    DIRECT_URL='postgresql://build:build@localhost:5432/build' \
    prisma generate

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
