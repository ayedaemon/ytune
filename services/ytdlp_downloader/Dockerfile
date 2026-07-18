FROM python:3.12-slim

# ffmpeg for audio extraction; curl+unzip to install deno, which yt-dlp needs as a
# JS runtime to solve YouTube's signature/n-challenges on authenticated requests —
# without it, cookie-authenticated extraction gets SABR-only formats and fails
# with "Requested format is not available" (https://github.com/yt-dlp/yt-dlp/wiki/EJS).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY core/ ./core/
COPY services/ytdlp_downloader/requirements.txt ./requirements.txt
RUN uv pip install --system -r requirements.txt

COPY services/ytdlp_downloader/ ./services/ytdlp_downloader/

ENV PYTHONPATH=/app

CMD ["python", "-m", "services.ytdlp_downloader.main"]
