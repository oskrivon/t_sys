FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    ccxt pandas numpy pydantic pydantic-settings structlog python-dotenv \
    scikit-learn joblib matplotlib openai python-telegram-bot \
    websockets aiohttp redis pyyaml

# Copy source
COPY src/ src/
COPY scripts/ scripts/
COPY config/ config/
