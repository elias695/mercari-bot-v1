FROM python:3.11-slim

WORKDIR /app

# Chrome + Chromium + toutes les dépendances
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    libjpeg-dev \
    libpng-dev \
    libwebp-dev \
    libglib2.0-0 \
    libnss3 \
    libfontconfig1 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY reference_images/ ./reference_images/

RUN echo "=== Images ===" && ls /app/reference_images/ | wc -l && echo "images copiées"

CMD ["python", "-u", "bot.py"]
