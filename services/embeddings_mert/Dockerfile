FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 ffmpeg git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY core/ ./core/
COPY services/embeddings_mert/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt

# Bake MERT-v1-95M into the image at build time. snapshot_download caches to
# ~/.cache/huggingface/hub; subsequent AutoModel.from_pretrained calls find the
# local snapshot and skip the network entirely.
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('m-a-p/MERT-v1-95M')"

ENV NUMBA_CPU_NAME=generic
ENV PYTHONPATH=/app

COPY services/embeddings_mert/ ./services/embeddings_mert/

CMD ["python", "-m", "services.embeddings_mert.main"]
