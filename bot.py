import os
import time
import json
import logging
import requests
from pathlib import Path
from datetime import datetime
from PIL import Image
import imagehash
from io import BytesIO
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)
 
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "8743642480:AAH5YC1X9042v80WZfNxcZhMqVWRxPJicxw")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6886739401")
SIMILARITY_THRESHOLD = int(os.getenv("SIMILARITY_THRESHOLD", "15"))
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL", "120"))
REFERENCE_DIR    = Path(os.getenv("REFERENCE_DIR", "reference_images"))
SEEN_FILE        = Path("seen_items.json")
 
KEYWORDS = [
    "ナイキ ランニング",
    "nike running",
    "アンダーアーマー ランニング",
    "under armour running",
]
 
def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()
 
def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))
 
def load_reference_hashes() -> list:
    refs = []
    REFERENCE_DIR.mkdir(exist_ok=True)
    files = list(REFERENCE_DIR.glob("*.jpg")) + list(REFERENCE_DIR.glob("*.jpeg")) + list(REFERENCE_DIR.glob("*.png"))
    if not files:
        log.warning(f"Aucune image de reference dans '{REFERENCE_DIR}/'")
        return refs
    log.info(f"Chargement de {len(files)} images...")
    for f in files:
        try:
            img = Image.open(f).convert("RGB")
            h = imagehash.phash(img)
            refs.append({"path": str(f), "hash": h})
        except Exception as e:
            log.warning(f"Erreur {f}: {e}")
    log.info(f"{len(refs)} hashes charges.")
    return refs
 
def fetch_image(url: str):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGB")
    except:
        return None
 
def is_similar(item_img, refs, threshold):
    if not refs:
        return False, 99, ""
    item_hash = imagehash.phash(item_img)
    best_dist = 999
    best_ref = ""
    for ref in refs:
        dist = item_hash - ref["hash"]
        if dist < best_dist:
            best_dist = dist
            best_ref = ref["path"]
    return best_dist <= threshold, best_dist, best_ref
 
MERCARI_URL = "https://api.mercari.jp/search_index/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
    "Accept": "application/json",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Origin": "https://jp.mercari.com",
    "Referer": "https://jp.mercari.com/",
    "X-Platform": "web",
}
 
def search_mercari(keyword, limit=30):
    params = {"keyword": keyword, "limit": limit, "offset": 0, "sort": "created_time", "order": "desc", "status": "on_sale"}
    try:
        r = requests.get(MERCARI_URL, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        items = r.json().get("items", [])
        log.info(f"  '{keyword}' -> {len(items)} articles")
        return items
    except Exception as e:
        log.warning(f"Erreur Mercari '{keyword}': {e}")
        return []
 
def item_info(item):
    return {
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "price": item.get("price", 0),
        "image_url": (item.get("thumbnails") or [None])[0] or "",
        "url": f"https://jp.mercari.com/item/{item.get('id', '')}",
    }
 
def send_telegram(text, image_url=""):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if image_url:
        try:
            r = requests.post(f"{base}/sendPhoto", data={"chat_id": TELEGRAM_CHAT_ID, "photo": image_url, "caption": text, "parse_mode": "HTML"}, timeout=10)
            if r.ok:
                return
        except:
            pass
    requests.post(f"{base}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
 
def notify(item, dist, ref_path, keyword):
    score_pct = max(0, 100 - int(dist * 100 / 64))
    msg = (
        f"🔥 <b>Match trouve !</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👕 <b>{item['name']}</b>\n"
        f"💴 <b>{item['price']:,} ¥</b>\n"
        f"🔍 Mot-cle : <code>{keyword}</code>\n"
        f"📊 Similarite : <b>{score_pct}%</b>\n"
        f"🖼 Reference : <code>{Path(ref_path).name if ref_path else 'N/A'}</code>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>"
    )
    log.info(f"  -> MATCH {item['name']} (dist={dist})")
    send_telegram(msg, image_url=item["image_url"])
 
def run():
    log.info("=== Mercari JP Bot demarre ===")
    refs = load_reference_hashes()
    seen = load_seen()
    send_telegram(
        f"✅ <b>Bot demarre !</b>\n"
        f"📸 {len(refs)} images de reference\n"
        f"🔎 {len(KEYWORDS)} mots-cles surveilles\n"
        f"⏱ Scan toutes les {SCAN_INTERVAL // 60} min"
    )
    while True:
        log.info(f"--- Scan {datetime.now().strftime('%H:%M:%S')} ---")
        refs = load_reference_hashes()
        for keyword in KEYWORDS:
            items = search_mercari(keyword)
            for raw in items:
                info = item_info(raw)
                if not info["id"] or info["id"] in seen:
                    continue
                seen.add(info["id"])
                if not refs:
                    notify(info, 0, "", keyword)
                    continue
                img = fetch_image(info["image_url"])
                if img is None:
                    continue
                matched, dist, ref_path = is_similar(img, refs, SIMILARITY_THRESHOLD)
                if matched:
                    notify(info, dist, ref_path, keyword)
            time.sleep(2)
        save_seen(seen)
        log.info(f"Prochain scan dans {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)
 
if __name__ == "__main__":
    run()
