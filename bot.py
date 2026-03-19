import os
import time
import json
import logging
from pathlib import Path
from datetime import datetime
from PIL import Image
import imagehash
import requests
from io import BytesIO
import mercari as mercari_api
from mercari import MercariSearchStatus, MercariSort, MercariOrder
 
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
        log.warning("Aucune image de reference")
        return refs
    for f in files:
        try:
            img = Image.open(f).convert("RGB")
            refs.append({"path": str(f), "hash": imagehash.phash(img)})
        except Exception as e:
            log.warning(f"Erreur {f}: {e}")
    log.info(f"{len(refs)} hashes charges.")
    return refs
 
def fetch_image(url: str):
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGB")
    except:
        return None
 
def is_similar(item_img, refs, threshold):
    if not refs:
        return False, 99, ""
    item_hash = imagehash.phash(item_img)
    best_dist, best_ref = 999, ""
    for ref in refs:
        dist = item_hash - ref["hash"]
        if dist < best_dist:
            best_dist, best_ref = dist, ref["path"]
    return best_dist <= threshold, best_dist, best_ref
 
def search_keyword(keyword: str, max_items: int = 30) -> list:
    results = []
    try:
        count = 0
        for item in mercari_api.search(
            keyword,
            sort=MercariSort.SORT_CREATED_TIME,
            order=MercariOrder.ORDER_DESC,
            status=MercariSearchStatus.ON_SALE,
        ):
            results.append({
                "id": item.id,
                "name": item.productName,
                "price": item.price,
                "image_url": item.imageURL,
                "url": item.productURL,
            })
            count += 1
            if count >= max_items:
                break
        log.info(f"  '{keyword}' -> {len(results)} articles ✅")
    except Exception as e:
        log.warning(f"  '{keyword}' -> erreur: {e}")
    return results
 
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
        f"🔎 {len(KEYWORDS)} mots-cles\n"
        f"⏱ Scan toutes les {SCAN_INTERVAL // 60} min\n"
        f"🔑 Auth: JWT auto ✅"
    )
 
    scan_count = 0
    while True:
        scan_count += 1
        log.info(f"--- Scan #{scan_count} {datetime.now().strftime('%H:%M:%S')} ---")
        refs = load_reference_hashes()
        total_found = 0
 
        for keyword in KEYWORDS:
            items = search_keyword(keyword, max_items=30)
            total_found += len(items)
            for info in items:
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
            time.sleep(3)
 
        save_seen(seen)
        log.info(f"Total: {total_found} articles | Prochain scan dans {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)
 
if __name__ == "__main__":
    run()
