FROM python:3.12-slim
RUN pip install --no-cache-dir rich requests
WORKDIR /app
COPY orphan_scan.py /app/orphan_scan.py
ENTRYPOINT ["python", "/app/orphan_scan.py"]
