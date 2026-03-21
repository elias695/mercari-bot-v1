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

# ═══════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_IDS       = [c.strip() for c in os.environ["TELEGRAM_CHAT_IDS"].split(",")]

_raw = float(os.getenv("SIMILARITY_THRESHOLD", "0.80"))
SIMILARITY_THRESHOLD = _raw / 100.0 if _raw > 1.0 else _raw

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "120"))
REFERENCE_DIR = Path(os.getenv("REFERENCE_DIR", "reference_images"))
SEEN_FILE     = Path("seen_items.json")
MAX_PRICE     = int(os.getenv("MAX_PRICE", "0"))
MIN_PRICE     = int(os.getenv("MIN_PRICE", "0"))

KEYWORDS = [
    "nike",
    "under armour",
    "ナイキ",
    "アンダーアーマー",
]

# ═══════════════════════════════════════════════════
#  PERSISTANCE
# ═══════════════════════════════════════════════════

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ═══════════════════════════════════════════════════
#  IMAGES DE RÉFÉRENCE
# ═══════════════════════════════════════════════════

def load_ref_hashes() -> list:
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        files.extend(REFERENCE_DIR.glob(ext))
    log.info(f"{len(files)} images de référence")
    hashes = []
    for f in sorted(files):
        try:
            img = Image.open(f).convert("RGB")
            hashes.append((f.name, imagehash.phash(img)))
        except Exception as e:
            log.warning(f"Image ignorée {f.name}: {e}")
    return hashes

def compare(img_url: str, ref_hashes: list) -> tuple:
    if not img_url or not ref_hashes:
        return 0.0, ""
    try:
        r = requests.get(img_url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        h = imagehash.phash(Image.open(BytesIO(r.content)).convert("RGB"))
        best_sim, best_ref = 0.0, ""
        for name, ref_h in ref_hashes:
            sim = 1.0 - (h - ref_h) / 64.0
            if sim > best_sim:
                best_sim, best_ref = sim, name
        return best_sim, best_ref
    except Exception:
        return 0.0, ""

# ═══════════════════════════════════════════════════
#  CHROME / SELENIUM
# ═══════════════════════════════════════════════════

def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=ja-JP")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
    opts.binary_location = "/usr/bin/chromium"
    service = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=opts)
    # Cache le fait que c'est Selenium
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en']});
            Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
        """
    })
    return driver


def fetch_items_selenium(driver, keyword: str) -> list:
    import urllib.parse, re, json as _json
    items = []

    params = {"keyword": keyword, "status": "on_sale",
               "sort": "created_time", "order": "desc"}
    if MIN_PRICE > 0:
        params["price_min"] = MIN_PRICE
    if MAX_PRICE > 0:
        params["price_max"] = MAX_PRICE

    url = "https://jp.mercari.com/search?" + urllib.parse.urlencode(params)

    try:
        driver.get(url)
        # Attend que la page charge (attend qu'un élément article apparaisse)
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    "[data-testid='item-cell'], li[data-location], mer-item-thumbnail"))
            )
        except Exception:
            pass  # Continue même si timeout — on essaie quand même d'extraire

        time.sleep(2)  # Laisse le JS s'exécuter

        html = driver.page_source

        # Extraction depuis __NEXT_DATA__
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
            html, re.DOTALL
        )
        if m:
            try:
                nd = _json.loads(m.group(1))
                raw = (
                    _dig(nd, "props","pageProps","initialState","search","items") or
                    _dig(nd, "props","pageProps","searchResult","items") or
                    _dig(nd, "props","pageProps","items") or
                    []
                )
                for it in raw:
                    parsed = _normalize(it, keyword)
                    if parsed:
                        items.append(parsed)
                if items:
                    log.info(f"  [__NEXT_DATA__] '{keyword}' → {len(items)}")
                    return items
            except Exception as e:
                log.debug(f"  __NEXT_DATA__ parse: {e}")

        # Fallback : extraction DOM directe
        cards = driver.find_elements(By.CSS_SELECTOR,
            "li[data-location], [data-testid='item-cell'], mer-item-thumbnail")
        for card in cards[:30]:
            try:
                link = card.find_element(By.TAG_NAME, "a")
                href = link.get_attribute("href") or ""
                item_id = href.split("/item/")[-1].split("?")[0] if "/item/" in href else ""
                if not item_id:
                    continue

                name_els = card.find_elements(By.CSS_SELECTOR,
                    "[class*='itemName'], [class*='name'], p")
                name = name_els[0].text.strip() if name_els else ""

                price_els = card.find_elements(By.CSS_SELECTOR,
                    "[class*='price'], mer-price, [class*='Price']")
                price_text = price_els[0].text if price_els else "0"
                price = int("".join(c for c in price_text if c.isdigit()) or "0")

                img_els = card.find_elements(By.TAG_NAME, "img")
                img_url = img_els[0].get_attribute("src") or "" if img_els else ""

                items.append({
                    "id":        item_id,
                    "name":      name,
                    "price":     price,
                    "image_url": img_url,
                    "url":       f"https://jp.mercari.com/item/{item_id}",
                    "keyword":   keyword,
                })
            except Exception:
                continue

        if items:
            log.info(f"  [DOM] '{keyword}' → {len(items)}")
        else:
            log.warning(f"  '{keyword}' → 0 articles (page bloquée ?)")

    except Exception as e:
        log.warning(f"  Selenium erreur '{keyword}': {e}")

    return items


def _dig(obj, *keys):
    for k in keys:
        if obj is None:
            return None
        obj = obj.get(k) if isinstance(obj, dict) else None
    return obj


def _normalize(raw: dict, keyword: str) -> dict | None:
    iid = str(raw.get("id") or raw.get("itemId") or "").strip()
    if not iid:
        return None
    if not iid.startswith("m"):
        iid = "m" + iid
    name = raw.get("name") or raw.get("itemName") or ""
    price = 0
    for pk in ("price", "itemPrice", "sellingPrice"):
        v = raw.get(pk)
        if v is not None:
            try:
                price = int(str(v).replace(",","").replace("¥","").strip())
                break
            except Exception:
                pass
    img = ""
    for ik in ("thumbnails", "photos", "images"):
        v = raw.get(ik)
        if isinstance(v, list) and v:
            img = v[0] if isinstance(v[0], str) else (v[0].get("imageUrl") or "")
            break
    if not img:
        for ik in ("thumbnail", "imageUrl", "image_url", "thumbnailUrl"):
            img = raw.get(ik, "")
            if img:
                break
    return {
        "id": iid, "name": name, "price": price,
        "image_url": img,
        "url": f"https://jp.mercari.com/item/{iid}",
        "keyword": keyword,
    }

# ═══════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════

def send_telegram(text: str, image_url: str = ""):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    for chat_id in CHAT_IDS:
        sent = False
        if image_url:
            try:
                r = requests.post(f"{base}/sendPhoto",
                    data={"chat_id": chat_id, "photo": image_url,
                          "caption": text, "parse_mode": "HTML"}, timeout=10)
                sent = r.ok
            except Exception:
                pass
        if not sent:
            try:
                requests.post(f"{base}/sendMessage",
                    data={"chat_id": chat_id, "text": text,
                          "parse_mode": "HTML"}, timeout=10)
            except Exception as ex:
                log.warning(f"Telegram erreur {chat_id}: {ex}")

def notify(item: dict, sim: float, ref: str):
    pct = f"{sim * 100:.1f}%"
    price_str = f"{item['price']:,}" if item['price'] else "—"
    msg = (
        f"🔥 <b>Match trouvé !</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👕 <b>{item['name']}</b>\n"
        f"💴 <b>{price_str} ¥</b>\n"
        f"🔍 Mot-clé : <i>{item['keyword']}</i>\n"
        f"📊 Similarité : <b>{pct}</b>\n"
        f"🖼 Référence : <code>{ref}</code>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>"
    )
    send_telegram(msg, image_url=item["image_url"])
    log.info(f"  ✅ {item['name'][:50]} ({pct})")

# ═══════════════════════════════════════════════════
#  BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════

def run():
    log.info("=== Mercari JP Bot (Chrome) ===")
    log.info(f"SIMILARITY_THRESHOLD: {SIMILARITY_THRESHOLD:.2f} ({SIMILARITY_THRESHOLD*100:.0f}%)")
    log.info(f"SCAN_INTERVAL: {SCAN_INTERVAL}s")

    seen      = load_seen()
    ref_hashes = load_ref_hashes()

    send_telegram(
        f"✅ <b>Bot démarré !</b>\n"
        f"🌐 Mode : <b>Chrome headless</b>\n"
        f"📸 <b>{len(ref_hashes)}</b> images de référence\n"
        f"🔍 <b>{len(KEYWORDS)}</b> mots-clés\n"
        f"📊 Seuil : <b>{SIMILARITY_THRESHOLD * 100:.0f}%</b>\n"
        f"👥 <b>{len(CHAT_IDS)}</b> destinataire(s)\n"
        f"⏱ Scan toutes les <b>{SCAN_INTERVAL // 60}min</b>"
    )

    scan_count = 0
    while True:
        scan_count += 1
        log.info(f"─── Scan #{scan_count} · {datetime.now().strftime('%d/%m %H:%M:%S')} ───")

        ref_hashes = load_ref_hashes()
        new_matches = 0
        seen_this   = set()

        # Un driver Chrome par scan (plus stable que le garder entre scans)
        driver = None
        try:
            driver = make_driver()
            # Visite la page d'accueil d'abord pour avoir les cookies
            driver.get("https://jp.mercari.com/")
            time.sleep(3)

            for keyword in KEYWORDS:
                items = fetch_items_selenium(driver, keyword)
                time.sleep(3)

                for item in items:
                    iid = item["id"]
                    if iid in seen or iid in seen_this:
                        continue
                    seen_this.add(iid)
                    seen.add(iid)

                    if not ref_hashes:
                        continue

                    sim, ref = compare(item["image_url"], ref_hashes)
                    log.debug(f"    {item['name'][:40]} sim={sim:.2f}")

                    if sim >= SIMILARITY_THRESHOLD:
                        notify(item, sim, ref)
                        new_matches += 1

        except Exception as e:
            log.error(f"Erreur scan: {e}")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

        save_seen(seen)
        log.info(f"Scan #{scan_count} — {new_matches} match(s) — prochain dans {SCAN_INTERVAL}s")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Arrêt manuel.")
    except Exception as e:
        log.critical(f"Erreur fatale: {e}", exc_info=True)
        try:
            send_telegram(f"🚨 <b>Bot crashé !</b>\n<code>{str(e)[:300]}</code>")
        except Exception:
            pass
        raise
