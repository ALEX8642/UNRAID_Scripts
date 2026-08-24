FROM python:3.12-slim
RUN pip install --no-cache-dir rich requests
WORKDIR /app
COPY dupe_review.py /app/dupe_review.py
ENTRYPOINT ["python", "/app/dupe_review.py"]
