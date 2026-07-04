FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    NETZSIM_HOST=0.0.0.0

WORKDIR /app

# System deps occasionally needed by pandapower's scientific stack
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml .
COPY src/ ./src/
COPY scripts/ ./scripts/

# Generate the bundled sample dataset if none is mounted at runtime.
RUN python scripts/generate_sample_data.py

EXPOSE 8000
CMD ["python", "-m", "netzsim.main"]
