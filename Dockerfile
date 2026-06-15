FROM python:3.11-slim

WORKDIR /app

COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY jarvis_cloud_web.py .

ENV PORT=8010
EXPOSE 8010

CMD ["sh", "-c", "uvicorn jarvis_cloud_web:app --host 0.0.0.0 --port ${PORT}"]
