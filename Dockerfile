FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libjpeg-dev libpng-dev libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    torch==2.2.2+cpu \
    torchvision==0.17.2+cpu \
    --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir \
    requests==2.31.0 \
    Pillow==10.3.0 \
    numpy==1.26.4

COPY preload_model.py .
RUN python3 preload_model.py

COPY bot.py .
COPY reference_images/ ./reference_images/

RUN echo "Images:" && ls /app/reference_images/ | wc -l

CMD ["python", "-u", "bot.py"]
