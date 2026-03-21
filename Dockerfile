FROM python:3.11-slim

WORKDIR /app

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
    git \
    && rm -rf /var/lib/apt/lists/*

# PyTorch CPU
RUN pip install --no-cache-dir \
    torch==2.2.2+cpu \
    torchvision==0.17.2+cpu \
    --index-url https://download.pytorch.org/whl/cpu

# Autres dépendances
RUN pip install --no-cache-dir \
    requests==2.31.0 \
    Pillow==10.3.0 \
    numpy==1.26.4 \
    selenium==4.18.1 \
    ftfy regex tqdm

# CLIP
RUN pip install --no-cache-dir git+https://github.com/openai/CLIP.git

COPY bot.py .
COPY reference_images/ ./reference_images/

# Pré-télécharge le modèle CLIP dans le cache Docker
# Le modèle est stocké dans /root/.cache/clip/
ENV TORCH_HOME=/app/.cache/torch
ENV CLIP_CACHE=/app/.cache/clip
RUN python3 -c "
import os
os.environ['TORCH_HOME'] = '/app/.cache/torch'
import clip
print('Téléchargement modèle CLIP ViT-B/32...')
model, preprocess = clip.load('ViT-B/32', device='cpu', download_root='/app/.cache/clip')
print('Modèle CLIP téléchargé et mis en cache OK')
"

RUN echo "Images:" && ls /app/reference_images/ | wc -l

CMD ["python", "-u", "bot.py"]
