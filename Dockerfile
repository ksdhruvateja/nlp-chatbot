# syntax=docker/dockerfile:1
FROM python:3.12-slim

# ffmpeg: video frame/audio extraction. tesseract: OCR for scanned PDFs.
# Optional build_ca secret supports trusted corporate HTTPS proxies.
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then \
      cat /run/secrets/build_ca >> /etc/ssl/certs/ca-certificates.crt; \
    fi \
 && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock.txt

COPY app ./app
COPY scripts ./scripts
COPY sql ./sql
COPY sample_knowledge ./sample_knowledge

RUN useradd --create-home --uid 10001 fixbridge \
 && mkdir -p /app/data/media \
 && chown -R fixbridge:fixbridge /app/data \
 && chmod -R a+rX /app
USER fixbridge

ENV PYTHONUNBUFFERED=1 RUN_WORKER_IN_PROCESS=false STORAGE_BACKEND=s3
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"

# API by default. Worker:  python -m scripts.worker   Crawler: python -m scripts.crawl_manuals
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
