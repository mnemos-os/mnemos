# MNEMOS-OS Dockerfile
# Multi-stage build with uv for fast dependency resolution (10x faster than pip)

# Stage 1: Builder with uv (fast dependency installation)
FROM python:3.11-slim as builder

WORKDIR /app

# Install system deps: build tools + asyncpg + psycopg + numpy + GPU detection
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev curl git && \
    rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package installer)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Copy pyproject.toml (uv will handle dependency resolution)
COPY pyproject.toml .
COPY requirements.txt .

# Use uv to create a thin virtual environment with all deps
# uv pip install is ~10x faster than pip; --system needed because we're not in a venv
RUN uv pip install --system -r requirements.txt

# Stage 2: Runtime (minimal footprint)
FROM python:3.11-slim

WORKDIR /app

# Install only runtime system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder (preserves installation with all deps)
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy application code
COPY . .

# Register the package itself so importlib.metadata.version("mnemos-os")
# matches pyproject.toml. --no-deps because deps are already installed
# from requirements.txt above.
RUN python -m pip install --no-deps --no-build-isolation .

# Environment variables
ENV PG_USER=mnemos_user \
    PG_DATABASE=mnemos \
    PG_HOST=postgres \
    INFERENCE_EMBED_HOST=http://ollama:11434 \
    MNEMOS_PORT=5002 \
    PYTHONUNBUFFERED=1

EXPOSE 5002

# Health check (optional)
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5002/health').read()" || exit 1

CMD ["python", "-m", "uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "5002", "--workers", "1"]
