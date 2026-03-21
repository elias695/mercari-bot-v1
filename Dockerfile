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

# Installe PyTorch CPU d'abord (depuis le bon index)
RUN pip install --no-cache-dir \
    torch==2.2.2+cpu \
    torchvision==0.17.2+cpu \
    --index-url https://download.pytorch.org/whl/cpu

# Installe CLIP et le reste
RUN pip install --no-cache-dir \
    requests==2.31.0 \
    Pillow==10.3.0 \
    numpy==1.26.4 \
    selenium==4.18.1 \
    ftfy \
    regex \
    tqdm

RUN pip install --no-cache-dir git+https://github.com/openai/CLIP.git

COPY bot.py .
COPY reference_images/ ./reference_images/

RUN echo "=== Images ===" && ls /app/reference_images/ | wc -l && echo "images"

# Pré-télécharge le modèle CLIP au build (évite de le télécharger au runtime)
RUN python3 -c "import clip; clip.load('ViT-B/32', device='cpu'); print('CLIP modèle OK')"

CMD ["python", "-u", "bot.py"]
