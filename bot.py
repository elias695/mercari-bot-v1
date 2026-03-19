import os
import time
import json
import hashlib
import logging
import requests
import numpy as np
from pathlib import Path
from datetime import datetime
from PIL import Image
from io import BytesIO
import torch
import clip
from sklearn.metrics.pairwise import cosine_similarity

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "8743642480:AAH5YC1X9042v80WZfNxcZhMqVWRxPJicxw")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6886739401")
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.80"))
SCAN_INTERVAL   = int(os.getenv("SCAN_INTERVAL", "120"))   # 2 minutes
REFERENCE_DIR   = Path(os.getenv("REFERENCE_DIR", "reference_images"))
SEEN_FILE       = Path("seen_items.json")

# Mercari JP search keywords
KEYWORDS = [
    "ナイキ ランニング",          # Nike Running (JP)
    "nike running",              # Nike Running (EN)
    "アンダーアーマー",           # Under Armour (JP)
    "under armour running",      # Under Armour (EN)
]

# ─── Load CLIP model ─────────────────────────────────────────────────────────
log.info("Loading CLIP model...")
device = "cuda" if torch.cuda.is_available() else "cpu"
model, preprocess = clip.load("ViT-B/32", device=device)
log.info(f"CLIP loaded on {device}")

# ─── Seen items cache ────────────────────────────────────────────────────────
def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ─── Image helpers ───────────────────────────────────────────────────────────
def get_image_embedding(img: Image.Image) -> np.ndarray:
    """Return CLIP embedding for a PIL image."""
    tensor = preprocess(img).unsqueeze(0).to(device)
    with torch.no_grad():
        emb = model.encode_image(tensor)
    return emb.cpu().numpy()

def load_reference_embeddings() -> list[dict]:
    """Load all reference images and compute their embeddings."""
    refs = []
    REFERENCE_DIR.mkdir(exist_ok=True)
    files = list(REFERENCE_DIR.glob("*.jpg")) + \
            list(REFERENCE_DIR.glob("*.jpeg")) + \
            list(REFERENCE_DIR.glob("*.png")) + \
            list(REFERENCE_DIR.glob("*.webp"))
    if not files:
        log.warning(f"No reference images found in '{REFERENCE_DIR}/'")
        return refs
    log.info(f"Loading {len(files)} reference images...")
    for f in files:
        try:
            img = Image.open(f).convert("RGB")
            emb = get_image_embedding(img)
            refs.append({"path": str(f), "embedding": emb})
        except Exception as e:
            log.warning(f"Could not load {f}: {e}")
    log.info(f"Loaded {len(refs)} reference embeddings.")
    return refs

def fetch_image(url: str) -> Image.Image | None:
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as e:
        log.warning(f"Cannot fetch image {url}: {e}")
        return None

def is_similar(item_img: Image.Image, refs: list[dict], threshold: float) -> tuple[bool, float, str]:
    """Check if item_img is similar to any reference image."""
    if not refs:
        return False, 0.0, ""
    item_emb = get_image_embedding(item_img)
    best_score = 0.0
    best_ref   = ""
    for ref in refs:
        score = cosine_similarity(item_emb, ref["embedding"])[0][0]
        if score > best_score:
            best_score = score
            best_ref   = ref["path"]
    matched = best_score >= threshold
    return matched, float(best_score), best_ref

# ─── Mercari JP API ──────────────────────────────────────────────────────────
MERCARI_SEARCH_URL = "https://api.mercari.jp/search_index/search"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Origin": "https://jp.mercari.com",
    "Referer": "https://jp.mercari.com/",
    "X-Platform": "web",
}

def search_mercari(keyword: str, limit: int = 30) -> list[dict]:
    """Search Mercari JP and return list of items."""
    params = {
        "keyword": keyword,
        "limit": limit,
        "offset": 0,
        "sort": "created_time",
        "order": "desc",
        "status": "on_sale",
    }
    try:
        r = requests.get(MERCARI_SEARCH_URL, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        log.info(f"  '{keyword}' → {len(items)} items")
        return items
    except Exception as e:
        log.warning(f"Mercari search failed for '{keyword}': {e}")
        return []

def item_to_info(item: dict) -> dict:
    """Extract relevant fields from a Mercari item."""
    return {
        "id":        item.get("id", ""),
        "name":      item.get("name", ""),
        "price":     item.get("price", 0),
        "image_url": item.get("thumbnails", [None])[0] or item.get("photo", {}).get("image_url", ""),
        "url":       f"https://jp.mercari.com/item/{item.get('id', '')}",
        "seller":    item.get("seller", {}).get("name", ""),
    }

# ─── Telegram ────────────────────────────────────────────────────────────────
def send_telegram(text: str, image_url: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured.")
        return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if image_url:
        try:
            requests.post(f"{base}/sendPhoto", data={
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": image_url,
                "caption": text,
                "parse_mode": "HTML",
            }, timeout=10)
            return
        except Exception:
            pass
    requests.post(f"{base}/sendMessage", data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=10)

def notify(item: dict, score: float, ref_path: str, keyword: str):
    msg = (
        f"🔥 <b>Match trouvé !</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👕 <b>{item['name']}</b>\n"
        f"💴 <b>{item['price']:,} ¥</b>\n"
        f"🔍 Mot-clé : <code>{keyword}</code>\n"
        f"📊 Similarité : <b>{score:.1%}</b>\n"
        f"🖼 Référence : <code>{Path(ref_path).name}</code>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>"
    )
    log.info(f"  → MATCH {item['name']} ({score:.1%})")
    send_telegram(msg, image_url=item["image_url"])

# ─── Main loop ───────────────────────────────────────────────────────────────
def run():
    log.info("=== Mercari JP Bot démarré ===")
    refs = load_reference_embeddings()
    seen = load_seen()

    send_telegram(
        f"✅ <b>Bot démarré</b>\n"
        f"📸 {len(refs)} images de référence chargées\n"
        f"🔎 {len(KEYWORDS)} mots-clés surveillés\n"
        f"⏱ Scan toutes les {SCAN_INTERVAL//60} min"
    )

    while True:
        log.info(f"--- Scan {datetime.now().strftime('%H:%M:%S')} ---")

        # Reload refs in case new images were added
        refs = load_reference_embeddings()

        for keyword in KEYWORDS:
            items = search_mercari(keyword)
            for raw in items:
                info = item_to_info(raw)
                if not info["id"] or info["id"] in seen:
                    continue
                seen.add(info["id"])

                if not refs:
                    # No refs → notify all new items
                    notify(info, 1.0, "", keyword)
                    continue

                img = fetch_image(info["image_url"])
                if img is None:
                    continue

                matched, score, ref_path = is_similar(img, refs, SIMILARITY_THRESHOLD)
                if matched:
                    notify(info, score, ref_path, keyword)

            time.sleep(2)  # be polite between keyword searches

        save_seen(seen)
        log.info(f"Prochain scan dans {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
