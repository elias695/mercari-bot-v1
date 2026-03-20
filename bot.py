import os, time, json, logging, requests
from pathlib import Path
from datetime import datetime
from PIL import Image
import imagehash
from io import BytesIO
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
 
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
 
def get_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ja-JP")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    options.binary_location = "/usr/bin/chromium"
    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)
 
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
    log.info(f"{len(refs)} images chargees.")
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
 
def search_mercari(driver, keyword):
    items = []
    try:
        url = f"https://jp.mercari.com/search?keyword={requests.utils.quote(keyword)}&status=on_sale&sort=created_time&order=desc"
        driver.get(url)
        
        # Wait for items to load
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='item-cell'], mer-item-thumbnail, li[data-location]"))
        )
        time.sleep(2)
        
        # Extract from page source JSON
        page_source = driver.page_source
        import re
        match = re.search(r'"items"\s*:\s*(\[[\s\S]*?\])\s*,\s*"[a-z]', page_source)
        if match:
            try:
                raw = json.loads(match.group(1))
                for item in raw[:30]:
                    if isinstance(item, dict) and item.get("id"):
                        items.append({
                            "id": str(item.get("id", "")),
                            "name": item.get("name", ""),
                            "price": item.get("price", 0),
                            "image_url": (item.get("thumbnails") or [None])[0] or "",
                            "url": f"https://jp.mercari.com/item/{item.get('id', '')}",
                        })
            except: pass
        
        # Fallback: parse DOM elements directly
        if not items:
            cards = driver.find_elements(By.CSS_SELECTOR, "li[data-location], [data-testid='item-cell']")
            for card in cards[:30]:
                try:
                    link = card.find_element(By.TAG_NAME, "a")
                    href = link.get_attribute("href") or ""
                    item_id = href.split("/item/")[-1].split("?")[0] if "/item/" in href else ""
                    name_el = card.find_elements(By.CSS_SELECTOR, "[class*='name'], [class*='title'], p")
                    name = name_el[0].text if name_el else ""
                    price_el = card.find_elements(By.CSS_SELECTOR, "[class*='price'], mer-price")
                    price_text = price_el[0].text.replace("¥", "").replace(",", "").strip() if price_el else "0"
                    price = int("".join(filter(str.isdigit, price_text))) if price_text else 0
                    img_el = card.find_elements(By.TAG_NAME, "img")
                    img_url = img_el[0].get_attribute("src") or "" if img_el else ""
                    if item_id:
                        items.append({"id": item_id, "name": name, "price": price, "image_url": img_url, "url": f"https://jp.mercari.com/item/{item_id}"})
                except: pass
 
        log.info(f"  '{keyword}' -> {len(items)} articles ✅")
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
        f"🔍 <code>{keyword}</code>\n📊 <b>{score}%</b>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>",
        image_url=item["image_url"]
    )
 
def run():
    log.info("=== Mercari JP Bot demarre (Selenium) ===")
    refs = load_refs()
    seen = load_seen()
    
    log.info("Lancement du navigateur Chrome...")
    driver = get_driver()
    log.info("Chrome lance ✅")
 
    send_telegram(
        f"✅ <b>Bot demarre !</b>\n"
        f"📸 {len(refs)} images\n"
        f"🔎 {len(KEYWORDS)} mots-cles\n"
        f"⏱ Scan toutes les {SCAN_INTERVAL//60} min\n"
        f"🌐 Mode: Chrome headless ✅"
    )
 
    scan_count = 0
    while True:
        scan_count += 1
        log.info(f"--- Scan #{scan_count} {datetime.now().strftime('%H:%M:%S')} ---")
        refs = load_refs()
        total = 0
 
        for keyword in KEYWORDS:
            items = search_mercari(driver, keyword)
            total += len(items)
            for info in items:
                if not info["id"] or info["id"] in seen: continue
                seen.add(info["id"])
                if not refs: notify(info, 0, "", keyword); continue
                img = fetch_image(info["image_url"])
                if img is None: continue
                matched, dist, ref_path = is_similar(img, refs, SIMILARITY_THRESHOLD)
                if matched: notify(info, dist, ref_path, keyword)
            time.sleep(5)
 
        save_seen(seen)
        log.info(f"Total: {total} | Prochain scan dans {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)
 
if __name__ == "__main__":
    run()
