FROM python:3.11-slim

# poppler-utils fornece o pdfimages (extrair fotos do PDF)
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# Render injeta a porta em $PORT
ENV PORT=8000
CMD uvicorn main:app --host 0.0.0.0 --port $PORT
