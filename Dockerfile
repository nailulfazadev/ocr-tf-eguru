FROM python:3.12-slim

# Install dependency sistem (Tesseract OCR + data bahasa Indonesia)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-ind \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Salin dependencies & install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin source code
COPY . .

# Buat direktori data konfigurasi
RUN mkdir -p /root/.bukti_tf

EXPOSE 8000

CMD ["uvicorn", "web_server:app", "--host", "0.0.0.0", "--port", "8000"]
