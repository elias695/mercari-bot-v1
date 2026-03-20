FROM python:3.11-slim

WORKDIR /app

# Dépendances système pour Pillow
RUN apt-get update && apt-get install -y \
    libjpeg-dev \
    libpng-dev \
    libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Copie le dossier reference_images S'IL EXISTE dans le repo
# (le || true évite l'erreur si le dossier est vide)
COPY reference_images/ ./reference_images/

# Affiche ce qui a été copié pour debug dans les logs de build
RUN echo "=== Images copiées ===" && ls -la /app/reference_images/ | head -20 && echo "Total: $(ls /app/reference_images/*.jpg /app/reference_images/*.jpeg /app/reference_images/*.png /app/reference_images/*.webp 2>/dev/null | wc -l) images"

CMD ["python", "-u", "bot.py"]
