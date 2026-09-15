# Backend image for the Docker/WSL2 + NVIDIA deployment.
# CUDA 12.2 + cuDNN 8 runtime matches the pinned ctranslate2==4.4.0
# (the last ctranslate2 line built against cuDNN 8). Pin release images by
# tag or digest; never use `latest` for a release.
FROM nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.11 python3.11-venv python3-pip ffmpeg gosu ca-certificates \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-cuda.txt ./
RUN python -m pip install -r requirements-cuda.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd -r -u 10001 -m appuser \
    && mkdir -p /app/data /app/models \
    && chown -R appuser:appuser /app

# The frontend container serves the UI; this image serves only the API.
ENV FRONTEND_DIST=/nonexistent \
    APP_HOST=0.0.0.0 APP_PORT=8000 \
    DATA_DIR=/app/data MODEL_CACHE_DIR=/app/models

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
