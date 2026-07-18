FROM python:3.12-slim

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY core/ ./core/
COPY services/ytm_sync/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt

COPY services/ytm_sync/ ./services/ytm_sync/

ENV PYTHONPATH=/app

CMD ["python", "-m", "services.ytm_sync.main"]
