FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build deps μόνο αν τα χρειάζεσαι (κρατά τα, ok)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Όλος ο κώδικας
COPY . .

# Compile-time έλεγχος συντακτικών λαθών
# Αντί για heredoc, βάλε αυτό:
RUN python -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ['main.py','tools.py','api_clients.py','config.py','intents.py','constants.py']]; print('✔ py_compile OK')"

ENV PYTHONPATH=/app
ENV OPENAI_AGENTS_DISABLE_TRACING=1
# Το PORT το δίνει το Cloud Run
ENV PORT=8080

# Εκκίνηση (bind στο $PORT)
CMD ["sh", "-c", "gunicorn -k uvicorn.workers.UvicornWorker -w 1 -b 0.0.0.0:${PORT:-8080} main:app"]
