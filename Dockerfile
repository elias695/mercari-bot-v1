FROM python:3.11-slim

WORKDIR /app

# Dépendances système pour Pillow (traitement d'images)
RUN apt-get update && apt-get install -y \
    libjpeg-dev \
    libpng-dev \
    libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Crée le dossier d'images si absent (les images sont montées via Railway volumes)
RUN mkdir -p reference_images

CMD ["python", "-u", "bot.py"]
