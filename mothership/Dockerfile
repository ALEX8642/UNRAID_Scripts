FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends bash curl jq \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir rich requests
WORKDIR /app
COPY common.py mothership.py cross-disk-dedupe.sh dupe_review.py orphan_scan.py /app/
RUN chmod +x /app/cross-disk-dedupe.sh
ENTRYPOINT ["python", "/app/mothership.py"]
