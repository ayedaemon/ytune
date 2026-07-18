FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 ffmpeg git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY core/ ./core/
COPY services/embeddings_clap/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt

# Bake the CLAP checkpoint into the image at build time. load_ckpt() without args
# downloads 630k-audioset-best.pt into the package dir and checks existence before
# re-downloading, so runtime calls are instant cache hits against this baked layer.
RUN python -c "import laion_clap; m = laion_clap.CLAP_Module(enable_fusion=False); m.load_ckpt()"

COPY services/embeddings_clap/ ./services/embeddings_clap/

ENV PYTHONPATH=/app

CMD ["python", "-m", "services.embeddings_clap.main"]
