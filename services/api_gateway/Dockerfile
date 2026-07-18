FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

RUN pip install uv --no-cache-dir

# Copy shared core first — changes here invalidate this layer
COPY core/ ./core/

# Service deps
COPY services/api_gateway/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt

# Service code
COPY services/api_gateway/ ./services/api_gateway/

ENV PYTHONPATH=/app

CMD ["uvicorn", "services.api_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
