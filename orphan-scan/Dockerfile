FROM python:3.12-slim
RUN pip install --no-cache-dir rich requests
WORKDIR /app
COPY common.py orphan_scan.py /app/
ENTRYPOINT ["python", "/app/orphan_scan.py"]
