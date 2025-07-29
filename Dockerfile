# Dockerfile

FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Shell-form CMD για expansion του $PORT
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 main:app
