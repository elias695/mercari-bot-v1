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
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
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
 
def get_mercari_token(session: requests.Session) -> str:
    """Get CSRF/auth token from Mercari JP homepage."""
    try:
        r = session.get("https://jp.mercari.com/", timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        # Extract dpop or auth token if present
        return ""
    except:
        return ""
 
def search_mercari(session: requests.Session, keyword: str, limit: int = 30) -> list:
    """Search using Mercari's search endpoint with proper session cookies."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://jp.mercari.com",
        "Referer": f"https://jp.mercari.com/search?keyword={requests.utils.quote(keyword)}&status=on_sale",
        "X-Platform": "web",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }
    
    # Try v2 search endpoint
    endpoints = [
        "https://api.mercari.jp/search_index/search",
        "https://api.mercari.jp/v2/search_index/search",
    ]
    
    params = {
        "keyword": keyword,
        "limit": limit,
        "offset": 0,
        "sort": "created_time",
        "order": "desc",
        "status": "on_sale",
        "t__time": int(time.time()),
    }
    
    for url in endpoints:
        try:
            r = session.get(url, headers=headers, params=params, timeout=15)
            if r.status_code == 200:
                items = r.json().get("items", [])
                log.info(f"  '{keyword}' -> {len(items)} articles")
                return items
            elif r.status_code == 401:
                log.warning(f"  '{keyword}' -> 401 (endpoint {url})")
        except Exception as e:
            log.warning(f"Erreur '{keyword}': {e}")
    
    # Fallback: scrape search page HTML and parse JSON embedded
    try:
        search_url = f"https://jp.mercari.com/search?keyword={requests.utils.quote(keyword)}&status=on_sale&sort=created_time&order=desc"
        r = session.get(search_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        }, timeout=15)
        
        if r.status_code == 200:
            # Try to find JSON data embedded in the page
            import re
            # Look for __NEXT_DATA__ or similar
            match = re.search(r'"items"\s*:\s*(\[.*?\])', r.text, re.DOTALL)
            if match:
                items = json.loads(match.group(1))
                log.info(f"  '{keyword}' (HTML) -> {len(items)} articles")
                return items
        log.warning(f"  '{keyword}' -> scraping echoue (status {r.status_code})")
    except Exception as e:
        log.warning(f"  '{keyword}' -> scraping erreur: {e}")
    
    return []
 
def item_info(item):
    return {
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "price": item.get("price", 0),
        "image_url": (item.get("thumbnails") or [None])[0] or item.get("photo", {}).get("image_url", ""),
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
    
    # Create persistent session (keeps cookies between requests)
    session = requests.Session()
    
    # Visit homepage first to get cookies
    log.info("Initialisation session Mercari JP...")
    try:
        session.get("https://jp.mercari.com/", timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })
        log.info("Session initialisee.")
    except Exception as e:
        log.warning(f"Init session: {e}")
 
    send_telegram(
        f"✅ <b>Bot demarre !</b>\n"
        f"📸 {len(refs)} images de reference\n"
        f"🔎 {len(KEYWORDS)} mots-cles surveilles\n"
        f"⏱ Scan toutes les {SCAN_INTERVAL // 60} min"
    )
 
    scan_count = 0
    while True:
        scan_count += 1
        log.info(f"--- Scan #{scan_count} {datetime.now().strftime('%H:%M:%S')} ---")
        refs = load_reference_hashes()
        
        # Refresh session every 10 scans
        if scan_count % 10 == 0:
            try:
                session.get("https://jp.mercari.com/", timeout=15, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
                    "Accept-Language": "ja-JP,ja;q=0.9",
                })
            except:
                pass
 
        total_found = 0
        for keyword in KEYWORDS:
            items = search_mercari(session, keyword)
            total_found += len(items)
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
            time.sleep(3)
 
        # Alert if still getting 0 results (API still blocked)
        if total_found == 0 and scan_count % 5 == 0:
            send_telegram("⚠️ <b>Attention</b> : 0 articles trouvés sur Mercari JP. L'API est peut-être bloquée.")
 
        save_seen(seen)
        log.info(f"Total articles trouves ce scan: {total_found} | Prochain scan dans {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)
 
if __name__ == "__main__":
    run()
