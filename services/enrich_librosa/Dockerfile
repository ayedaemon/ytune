FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY core/ ./core/
COPY services/enrich_librosa/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt

COPY services/enrich_librosa/ ./services/enrich_librosa/

ENV PYTHONPATH=/app
# Works around a numba/LLVM ARM64 codegen bug that segfaults inside librosa's
# @guvectorize beat-tracking code on this host's specific CPU model —
# https://github.com/numba/numba/issues/10388. Forces the generic ARM64 target
# instead of auto-detecting the (buggy) host CPU scheduling model.
ENV NUMBA_CPU_NAME=generic

CMD ["python", "-m", "services.enrich_librosa.main"]
