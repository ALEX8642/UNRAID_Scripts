FROM python:3.12-slim
RUN pip install --no-cache-dir rich requests
WORKDIR /app
COPY common.py dupe_review.py /app/
ENTRYPOINT ["python", "/app/dupe_review.py"]
