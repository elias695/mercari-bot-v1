import os, time, json, logging, requests, numpy as np, re
from pathlib import Path
from datetime import datetime
from PIL import Image
from io import BytesIO
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import torch
import torchvision.transforms as transforms

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_IDS       = [c.strip() for c in os.environ["TELEGRAM_CHAT_IDS"].split(",")]

_raw = float(os.getenv("SIMILARITY_THRESHOLD", "0.80"))
SIMILARITY_THRESHOLD = _raw / 100.0 if _raw > 1.0 else _raw

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "3600"))  # 1h par défaut
REFERENCE_DIR = Path(os.getenv("REFERENCE_DIR", "reference_images"))
SEEN_FILE     = Path("seen_items.json")
MAX_PRICE     = int(os.getenv("MAX_PRICE", "0"))
MIN_PRICE     = int(os.getenv("MIN_PRICE", "0"))

# ═══════════════════════════════════════════════════
#  DINOv2
# ═══════════════════════════════════════════════════

log.info("Chargement DINOv2...")

_transform = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                         std=[0.229, 0.224, 0.225]),
])

_model = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14")
_model.eval()
log.info("DINOv2 chargé ✅")


def crop_garment(img: Image.Image) -> Image.Image:
    """Rogne les barres d'interface et les fonds uniformes."""
    w, h = img.size
    img_array = np.array(img.convert("RGB"))
    gray = np.mean(img_array, axis=2)

    # Supprime les bandes noires (interface iPhone)
    top_crop, bottom_crop = 0, h
    for i in range(min(200, h)):
        if np.mean(gray[i, :]) < 30 or np.std(gray[i, :]) < 5:
            top_crop = i + 1
        else:
            break
    for i in range(h - 1, max(h - 200, 0), -1):
        if np.mean(gray[i, :]) < 30 or np.std(gray[i, :]) < 5:
            bottom_crop = i
        else:
            break
    if bottom_crop - top_crop < h * 0.5:
        top_crop, bottom_crop = 0, h

    img_cropped = img.crop((0, top_crop, w, bottom_crop))
    img_array2 = np.array(img_cropped.convert("RGB"))
    h2, w2 = img_array2.shape[:2]
    col_std = np.std(img_array2, axis=0).mean(axis=1)
    row_std = np.std(img_array2, axis=1).mean(axis=1)
    threshold = 8
    pad = 10
    left   = max(0, next((i for i in range(w2) if col_std[i] > threshold), 0) - pad)
    right  = min(w2, next((i for i in range(w2-1, 0, -1) if col_std[i] > threshold), w2) + pad)
    top2   = max(0, next((i for i in range(h2) if row_std[i] > threshold), 0) - pad)
    bottom2 = min(h2, next((i for i in range(h2-1, 0, -1) if row_std[i] > threshold), h2) + pad)
    if (right - left) < w2 * 0.3 or (bottom2 - top2) < h2 * 0.3:
        return img_cropped
    return img_cropped.crop((left, top2, right, bottom2))


def extract_features(img: Image.Image) -> np.ndarray:
    img_cropped = crop_garment(img)
    tensor = _transform(img_cropped).unsqueeze(0)
    with torch.no_grad():
        feat = _model(tensor).squeeze().numpy()
    norm = np.linalg.norm(feat)
    return feat / norm if norm > 0 else feat


def compare_with_ref(img_url: str, ref_name: str, ref_feat: np.ndarray) -> float:
    """Compare l'image d'un article avec UNE image de référence spécifique."""
    try:
        r = requests.get(img_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")
        feat = extract_features(img)
        sim = float(np.dot(feat, ref_feat))
        return (sim + 1.0) / 2.0
    except Exception:
        return 0.0

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

def load_ref_features() -> list:
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        files.extend(REFERENCE_DIR.glob(ext))
    log.info(f"{len(files)} images de référence")
    result = []
    for f in sorted(files):
        try:
            img = Image.open(f).convert("RGB")
            feat = extract_features(img)
            result.append((f.name, f, feat))
        except Exception as e:
            log.warning(f"Ignorée {f.name}: {e}")
    log.info(f"{len(result)} features calculées ✅")
    return result

# ═══════════════════════════════════════════════════
#  CHROME
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
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en']});
            Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
        """
    })
    return driver

# ═══════════════════════════════════════════════════
#  RECHERCHE PAR IMAGE MERCARI
# ═══════════════════════════════════════════════════

def search_by_image(driver, ref_path: Path) -> list:
    """
    Upload une image de référence sur Mercari JP et récupère
    les articles visuellement similaires retournés par Mercari.
    """
    items = []
    try:
        driver.get("https://jp.mercari.com/")
        time.sleep(3)

        # Trouve l'input file pour la recherche par image
        try:
            file_input = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    "input[type='file'][accept*='image'], input[accept='image/*']"
                ))
            )
            file_input.send_keys(str(ref_path.absolute()))
            log.info(f"  Image uploadée : {ref_path.name}")
            time.sleep(5)

            # Attend les résultats
            try:
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR,
                        "[data-testid='item-cell'], li[data-location], mer-item-thumbnail"
                    ))
                )
            except Exception:
                pass
            time.sleep(2)

        except Exception as e:
            log.warning(f"  Upload échoué pour {ref_path.name}: {e}")
            return []

        # Extrait les résultats depuis __NEXT_DATA__
        import json as _json
        html = driver.page_source
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
            html, re.DOTALL)
        if m:
            try:
                nd = _json.loads(m.group(1))
                raw = (
                    _dig(nd,"props","pageProps","initialState","search","items") or
                    _dig(nd,"props","pageProps","searchResult","items") or
                    _dig(nd,"props","pageProps","items") or []
                )
                for it in raw:
                    p = _norm(it, ref_path.name)
                    if p: items.append(p)
                if items:
                    log.info(f"  [NEXT] {ref_path.name} → {len(items)} articles")
                    return items
            except Exception:
                pass

        # Fallback DOM
        cards = driver.find_elements(By.CSS_SELECTOR,
            "li[data-location], [data-testid='item-cell'], mer-item-thumbnail")
        for card in cards[:30]:
            try:
                link = card.find_element(By.TAG_NAME, "a")
                href = link.get_attribute("href") or ""
                iid = href.split("/item/")[-1].split("?")[0] if "/item/" in href else ""
                if not iid: continue
                ne = card.find_elements(By.CSS_SELECTOR, "[class*='itemName'],[class*='name'],p")
                name = ne[0].text.strip() if ne else ""
                pe = card.find_elements(By.CSS_SELECTOR, "[class*='price'],mer-price")
                price = int("".join(c for c in (pe[0].text if pe else "0") if c.isdigit()) or "0")
                ie = card.find_elements(By.TAG_NAME, "img")
                img_url = ie[0].get_attribute("src") or "" if ie else ""
                items.append({"id": iid, "name": name, "price": price,
                              "image_url": img_url,
                              "url": f"https://jp.mercari.com/item/{iid}",
                              "ref": ref_path.name})
            except Exception:
                continue

        if items:
            log.info(f"  [DOM] {ref_path.name} → {len(items)} articles")
        else:
            log.warning(f"  {ref_path.name} → 0 résultats")

    except Exception as e:
        log.warning(f"  Erreur recherche image {ref_path.name}: {e}")

    return items


def _dig(obj, *keys):
    for k in keys:
        if obj is None: return None
        obj = obj.get(k) if isinstance(obj, dict) else None
    return obj


def _norm(raw, ref_name):
    iid = str(raw.get("id") or raw.get("itemId") or "").strip()
    if not iid: return None
    if not iid.startswith("m"): iid = "m" + iid
    name = raw.get("name") or raw.get("itemName") or ""
    price = 0
    for pk in ("price","itemPrice","sellingPrice"):
        v = raw.get(pk)
        if v is not None:
            try: price = int(str(v).replace(",","").replace("¥","").strip()); break
            except Exception: pass
    img = ""
    for ik in ("thumbnails","photos","images"):
        v = raw.get(ik)
        if isinstance(v, list) and v:
            img = v[0] if isinstance(v[0], str) else (v[0].get("imageUrl") or "")
            break
    if not img:
        for ik in ("thumbnail","imageUrl","image_url","thumbnailUrl"):
            img = raw.get(ik, "")
            if img: break
    return {"id": iid, "name": name, "price": price, "image_url": img,
            "url": f"https://jp.mercari.com/item/{iid}", "ref": ref_name}

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
            except Exception: pass
        if not sent:
            try:
                requests.post(f"{base}/sendMessage",
                    data={"chat_id": chat_id, "text": text,
                          "parse_mode": "HTML"}, timeout=10)
            except Exception as ex:
                log.warning(f"Telegram {chat_id}: {ex}")


def notify(item: dict, sim: float):
    pct = f"{sim*100:.1f}%"
    price_str = f"{item['price']:,}" if item['price'] else "—"
    msg = (
        f"🔥 <b>Match trouvé !</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👕 <b>{item['name']}</b>\n"
        f"💴 <b>{price_str} ¥</b>\n"
        f"🖼 Réf : <code>{item['ref']}</code>\n"
        f"📊 Similarité DINOv2 : <b>{pct}</b>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>"
    )
    send_telegram(msg, image_url=item["image_url"])
    log.info(f"  ✅ {item['name'][:50]} ({pct})")

# ═══════════════════════════════════════════════════
#  BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════

def run():
    log.info("=== Mercari Bot v2 — Recherche par Image uniquement ===")
    log.info(f"Seuil : {SIMILARITY_THRESHOLD*100:.0f}% | Intervalle : {SCAN_INTERVAL}s")

    seen = load_seen()
    ref_features = load_ref_features()

    if not ref_features:
        log.error("Aucune image de référence ! Arrêt.")
        return

    send_telegram(
        f"✅ <b>Bot v2 démarré !</b>\n"
        f"🧠 <b>Recherche par image + DINOv2</b>\n"
        f"📸 <b>{len(ref_features)}</b> images de référence\n"
        f"📊 Seuil : <b>{SIMILARITY_THRESHOLD*100:.0f}%</b>\n"
        f"👥 <b>{len(CHAT_IDS)}</b> destinataire(s)\n"
        f"⏱ Cycle toutes les <b>{SCAN_INTERVAL//3600}h{(SCAN_INTERVAL%3600)//60:02d}min</b>"
    )

    cycle = 0
    while True:
        cycle += 1
        log.info(f"═══ Cycle #{cycle} · {datetime.now().strftime('%d/%m %H:%M:%S')} ═══")
        new_matches = 0
        seen_this = set()

        driver = None
        try:
            driver = make_driver()

            for ref_name, ref_path, ref_feat in ref_features:
                log.info(f"── Recherche : {ref_name} ──")

                items = search_by_image(driver, ref_path)
                time.sleep(2)

                for item in items:
                    iid = item["id"]
                    if iid in seen or iid in seen_this:
                        continue
                    seen_this.add(iid)
                    seen.add(iid)

                    # Filtre prix
                    if MIN_PRICE > 0 and item["price"] < MIN_PRICE: continue
                    if MAX_PRICE > 0 and item["price"] > MAX_PRICE: continue

                    # Comparaison DINOv2 avec l'image de référence qui a servi à chercher
                    sim = compare_with_ref(item["image_url"], ref_name, ref_feat)
                    log.debug(f"    {item['name'][:40]} sim={sim:.2f}")

                    if sim >= SIMILARITY_THRESHOLD:
                        item["ref"] = ref_name
                        notify(item, sim)
                        new_matches += 1

        except Exception as e:
            log.error(f"Erreur cycle: {e}")
        finally:
            if driver:
                try: driver.quit()
                except Exception: pass

        save_seen(seen)
        log.info(f"Cycle #{cycle} terminé — {new_matches} match(s) — prochain dans {SCAN_INTERVAL}s")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Arrêt.")
    except Exception as e:
        log.critical(f"Fatal: {e}", exc_info=True)
        try: send_telegram(f"🚨 <b>Bot crashé !</b>\n<code>{str(e)[:300]}</code>")
        except Exception: pass
        raise
