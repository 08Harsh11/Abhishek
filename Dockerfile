FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py config.development.yaml .env ./
ENV APP_ENV=development APP_PORT=8232 APP_WORKERS=9 APP_LOG_LEVEL=info
EXPOSE 8232
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8232}"]
