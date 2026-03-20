import os, time, json, logging, requests, re
from pathlib import Path
from datetime import datetime
from PIL import Image
import imagehash
from io import BytesIO
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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
 
def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()
 
def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(list(seen)))
 
def load_refs():
    refs = []
    REFERENCE_DIR.mkdir(exist_ok=True)
    for f in list(REFERENCE_DIR.glob("*.jpg")) + list(REFERENCE_DIR.glob("*.jpeg")) + list(REFERENCE_DIR.glob("*.png")):
        try:
            refs.append({"path": str(f), "hash": imagehash.phash(Image.open(f).convert("RGB"))})
        except: pass
    log.info(f"{len(refs)} images de reference chargees.")
    return refs
 
def fetch_image(url):
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        return Image.open(BytesIO(r.content)).convert("RGB")
    except: return None
 
def is_similar(img, refs, threshold):
    if not refs: return False, 99, ""
    h = imagehash.phash(img)
    best, best_ref = 999, ""
    for ref in refs:
        d = h - ref["hash"]
        if d < best: best, best_ref = d, ref["path"]
    return best <= threshold, best, best_ref
 
def search_mercari(session, keyword):
    """Scrape Mercari JP search page and extract items from JSON embedded in HTML."""
    items = []
    url = f"https://jp.mercari.com/search?keyword={requests.utils.quote(keyword)}&status=on_sale&sort=created_time&order=desc"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }
    try:
        r = session.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            log.warning(f"  '{keyword}' -> HTTP {r.status_code}")
            return []
 
        # Extract JSON from __NEXT_DATA__ script tag
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.DOTALL)
        if not match:
            log.warning(f"  '{keyword}' -> __NEXT_DATA__ non trouve")
            return []
 
        data = json.loads(match.group(1))
        
        # Navigate the JSON structure to find items
        try:
            # Try common paths in Mercari's Next.js data
            page_props = data.get("props", {}).get("pageProps", {})
            
            # Path 1: direct items
            raw_items = page_props.get("items", [])
            
            # Path 2: search results
            if not raw_items:
                raw_items = page_props.get("searchResult", {}).get("items", [])
            
            # Path 3: initialState
            if not raw_items:
                initial = page_props.get("initialState", {})
                raw_items = initial.get("items", {}).get("items", [])
 
            for item in raw_items[:30]:
                items.append({
                    "id": str(item.get("id", "")),
                    "name": item.get("name", ""),
                    "price": item.get("price", 0),
                    "image_url": item.get("thumbnails", [None])[0] if item.get("thumbnails") else item.get("photo", {}).get("image_url", ""),
                    "url": f"https://jp.mercari.com/item/{item.get('id', '')}",
                })
            log.info(f"  '{keyword}' -> {len(items)} articles ✅")
        except Exception as e:
            log.warning(f"  '{keyword}' -> parsing erreur: {e}")
 
    except Exception as e:
        log.warning(f"  '{keyword}' -> erreur: {e}")
    return items
 
def send_telegram(text, image_url=""):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if image_url:
        try:
            r = requests.post(f"{base}/sendPhoto", data={"chat_id": TELEGRAM_CHAT_ID, "photo": image_url, "caption": text, "parse_mode": "HTML"}, timeout=10)
            if r.ok: return
        except: pass
    requests.post(f"{base}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
 
def notify(item, dist, ref_path, keyword):
    score = max(0, 100 - int(dist * 100 / 64))
    send_telegram(
        f"🔥 <b>Match trouve !</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"👕 <b>{item['name']}</b>\n💴 <b>{item['price']:,} ¥</b>\n"
        f"🔍 <code>{keyword}</code>\n📊 Similarite : <b>{score}%</b>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>",
        image_url=item["image_url"]
    )
 
def run():
    log.info("=== Mercari JP Bot demarre ===")
    refs = load_refs()
    seen = load_seen()
    session = requests.Session()
    
    # Init session with Mercari homepage
    try:
        session.get("https://jp.mercari.com/", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36", "Accept-Language": "ja-JP,ja;q=0.9"}, timeout=15)
        log.info("Session Mercari initialisee.")
    except: pass
 
    send_telegram(f"✅ <b>Bot demarre !</b>\n📸 {len(refs)} images\n🔎 {len(KEYWORDS)} mots-cles\n⏱ Scan toutes les {SCAN_INTERVAL//60} min\n🌐 Mode: HTML scraping")
 
    scan_count = 0
    while True:
        scan_count += 1
        log.info(f"--- Scan #{scan_count} {datetime.now().strftime('%H:%M:%S')} ---")
        refs = load_refs()
        total = 0
 
        for keyword in KEYWORDS:
            items = search_mercari(session, keyword)
            total += len(items)
            for info in items:
                if not info["id"] or info["id"] in seen: continue
                seen.add(info["id"])
                if not refs: notify(info, 0, "", keyword); continue
                img = fetch_image(info["image_url"])
                if img is None: continue
                matched, dist, ref_path = is_similar(img, refs, SIMILARITY_THRESHOLD)
                if matched: notify(info, dist, ref_path, keyword)
            time.sleep(4)
 
        save_seen(seen)
        log.info(f"Total: {total} articles | Prochain scan dans {SCAN_INTERVAL}s...")
        if total == 0 and scan_count % 5 == 0:
            send_telegram(f"⚠️ 0 articles trouves (scan #{scan_count}) — Mercari bloque peut-etre le scraping HTML.")
        time.sleep(SCAN_INTERVAL)
 
if __name__ == "__main__":
    run()
