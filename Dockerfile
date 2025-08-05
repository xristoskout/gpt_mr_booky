# ✅ Ελαφριά εικόνα python
FROM python:3.10-slim

# ✅ Ορισμός root φακέλου του app
WORKDIR /app

# ✅ Αντιγραφή μόνο των requirements για caching
COPY requirements.txt .

# ✅ Εγκατάσταση των dependencies
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# ✅ Αντιγραφή όλου του project (και των JSON knowledge files)
COPY . .

# ✅ Cloud Run ακούει στη θύρα 8080
EXPOSE 8080

# ✅ Εκκίνηση με gunicorn + uvicorn worker
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "2", "-b", "0.0.0.0:8080", "main:app"]
