FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    chromium chromium-driver \
    libjpeg-dev libpng-dev libwebp-dev \
    libglib2.0-0 libnss3 libfontconfig1 \
    libx11-6 libxcb1 libxcomposite1 libxcursor1 \
    libxdamage1 libxext6 libxfixes3 libxi6 \
    libxrandr2 libxrender1 libxss1 libxtst6 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# PyTorch CPU léger
RUN pip install --no-cache-dir \
    torch==2.2.2+cpu \
    torchvision==0.17.2+cpu \
    --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir \
    requests==2.31.0 \
    Pillow==10.3.0 \
    numpy==1.26.4 \
    selenium==4.18.1

COPY preload_model.py .
# Pré-télécharge MobileNetV2 (~14 Mo) au build
RUN python3 preload_model.py

COPY bot.py .
COPY reference_images/ ./reference_images/

RUN echo "Images:" && ls /app/reference_images/ | wc -l

CMD ["python", "-u", "bot.py"]
