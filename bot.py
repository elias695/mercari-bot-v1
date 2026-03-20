import os, time, json, logging, requests, re, urllib.parse
from pathlib import Path
from datetime import datetime
from PIL import Image
import imagehash
from io import BytesIO

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

# Mots-clés EN japonais pour l'API
KEYWORDS = [
    "ナイキ ランニング",
    "nike running",
    "アンダーアーマー ランニング",
    "under armour running",
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

    log.info(f"{len(files)} images de référence dans '{REFERENCE_DIR.absolute()}'")

    if not files:
        # Debug complet : montre tout ce qui existe dans /app
        for p in [Path("/app"), Path("/app/reference_images"), REFERENCE_DIR]:
            if p.exists():
                contents = list(p.iterdir())
                log.warning(f"Contenu de {p}: {[x.name for x in contents[:20]]}")
            else:
                log.warning(f"Dossier inexistant: {p}")
        return []

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
#  SCRAPING MERCARI — 3 méthodes en cascade
# ═══════════════════════════════════════════════════

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    # Initialise les cookies en visitant Mercari
    try:
        s.get("https://jp.mercari.com/", timeout=15)
    except Exception:
        pass
    return s


SESSION = _make_session()
_session_hits = 0


def _rotate_session():
    global SESSION, _session_hits
    SESSION = _make_session()
    _session_hits = 0
    log.info("Session réinitialisée")


def fetch_items(keyword: str) -> list:
    """Essaie 3 méthodes dans l'ordre jusqu'à obtenir des résultats."""
    global _session_hits
    _session_hits += 1
    if _session_hits > 20:
        _rotate_session()

    items = _method_fril(keyword)
    if items:
        return items

    items = _method_html(keyword)
    if items:
        return items

    items = _method_rss(keyword)
    return items


# ── Méthode 1 : API Fril/Mercari ancienne (souvent sans auth) ──────

def _method_fril(keyword: str) -> list:
    """
    Utilise l'ancienne API search de Mercari compatible avec
    les clients mobiles anciens — pas de DPoP requis.
    """
    try:
        params = {
            "keyword": keyword,
            "status": "on_sale",
            "sort_order": "created_time:desc",
            "limit": 30,
        }
        if MIN_PRICE > 0:
            params["price_min"] = MIN_PRICE
        if MAX_PRICE > 0:
            params["price_max"] = MAX_PRICE

        url = "https://jp.mercari.com/v1/api/search_index/items.json?" + urllib.parse.urlencode(params)
        resp = SESSION.get(url, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            raw = data.get("items", data.get("data", {}).get("items", []))
            items = [_normalize(it, keyword) for it in raw]
            items = [x for x in items if x]
            if items:
                log.info(f"  [méthode 1] '{keyword}' → {len(items)} articles")
            return items
    except Exception as e:
        log.debug(f"  méthode 1 échouée: {e}")
    return []


# ── Méthode 2 : Scraping HTML + extraction __NEXT_DATA__ ───────────

def _method_html(keyword: str) -> list:
    try:
        params = {
            "keyword": keyword,
            "status": "on_sale",
            "sort": "created_time",
            "order": "desc",
        }
        if MIN_PRICE > 0:
            params["price_min"] = MIN_PRICE
        if MAX_PRICE > 0:
            params["price_max"] = MAX_PRICE

        url = "https://jp.mercari.com/search?" + urllib.parse.urlencode(params)
        resp = SESSION.get(url, timeout=20)

        if resp.status_code in (403, 429):
            log.warning(f"  [méthode 2] bloqué ({resp.status_code}), pause 20s")
            time.sleep(20)
            _rotate_session()
            return []

        html = resp.text

        # Tente __NEXT_DATA__
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
            html, re.DOTALL
        )
        if m:
            try:
                nd = json.loads(m.group(1))
                # Cherche items dans différents chemins possibles
                raw = (
                    _dig(nd, "props","pageProps","initialState","search","items") or
                    _dig(nd, "props","pageProps","searchResult","items") or
                    _dig(nd, "props","pageProps","items") or
                    []
                )
                if raw:
                    items = [_normalize(it, keyword) for it in raw]
                    items = [x for x in items if x]
                    log.info(f"  [méthode 2 __NEXT_DATA__] '{keyword}' → {len(items)}")
                    return items
            except Exception as e:
                log.debug(f"  __NEXT_DATA__ parse: {e}")

        # Tente extraction regex directe dans le HTML
        ids = re.findall(r'"id"\s*:\s*"(m\d{10,})"', html)
        names = re.findall(r'"name"\s*:\s*"([^"]{5,80})"', html)
        prices = re.findall(r'"price"\s*:\s*(\d+)', html)
        thumbs = re.findall(r'"thumbnails"\s*:\s*\["([^"]+)"', html)

        items = []
        for i, iid in enumerate(ids[:30]):
            items.append({
                "id":        iid,
                "name":      names[i] if i < len(names) else "",
                "price":     int(prices[i]) if i < len(prices) else 0,
                "image_url": thumbs[i] if i < len(thumbs) else "",
                "url":       f"https://jp.mercari.com/item/{iid}",
                "keyword":   keyword,
            })
        if items:
            log.info(f"  [méthode 2 regex] '{keyword}' → {len(items)}")
        return items

    except Exception as e:
        log.debug(f"  méthode 2 échouée: {e}")
    return []


# ── Méthode 3 : RSS / sitemap Mercari ──────────────────────────────

def _method_rss(keyword: str) -> list:
    """
    Mercari propose des flux RSS pour certaines recherches.
    Dernier recours.
    """
    try:
        url = f"https://jp.mercari.com/search/rss?keyword={urllib.parse.quote(keyword)}&status=on_sale"
        resp = SESSION.get(url, timeout=15)
        if resp.status_code != 200:
            return []

        # Parse le XML RSS basiquement avec regex
        items = []
        entries = re.findall(r'<item>(.*?)</item>', resp.text, re.DOTALL)
        for entry in entries[:30]:
            link  = re.search(r'<link>([^<]+)</link>', entry)
            title = re.search(r'<title>([^<]+)</title>', entry)
            img   = re.search(r'<img[^>]+src="([^"]+)"', entry) or \
                    re.search(r'<enclosure[^>]+url="([^"]+)"', entry)

            if not link:
                continue
            href   = link.group(1).strip()
            iid    = href.rstrip("/").split("/")[-1]
            name   = title.group(1).strip() if title else ""
            imgurl = img.group(1).strip() if img else ""

            items.append({
                "id":        iid,
                "name":      name,
                "price":     0,
                "image_url": imgurl,
                "url":       href,
                "keyword":   keyword,
            })

        if items:
            log.info(f"  [méthode 3 RSS] '{keyword}' → {len(items)}")
        return items

    except Exception as e:
        log.debug(f"  méthode 3 échouée: {e}")
    return []


# ── Helpers ────────────────────────────────────────────────────────

def _dig(obj, *keys):
    for k in keys:
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and isinstance(k, int) and k < len(obj):
            obj = obj[k]
        else:
            return None
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
                price = int(str(v).replace(",", "").replace("¥", "").strip())
                break
            except (ValueError, TypeError):
                pass

    img = ""
    for ik in ("thumbnails", "photos", "images"):
        v = raw.get(ik)
        if isinstance(v, list) and v:
            img = v[0] if isinstance(v[0], str) else (v[0].get("imageUrl") or v[0].get("url") or "")
            break
    if not img:
        for ik in ("thumbnail", "imageUrl", "image_url", "photo", "thumbnailUrl"):
            img = raw.get(ik, "")
            if img:
                break

    return {
        "id":        iid,
        "name":      name,
        "price":     price,
        "image_url": img,
        "url":       f"https://jp.mercari.com/item/{iid}",
        "keyword":   keyword,
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
                r = requests.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": chat_id, "photo": image_url,
                          "caption": text, "parse_mode": "HTML"},
                    timeout=10,
                )
                sent = r.ok
            except Exception:
                pass
        if not sent:
            try:
                requests.post(
                    f"{base}/sendMessage",
                    data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                    timeout=10,
                )
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
    log.info("=== Mercari JP Bot ===")
    log.info(f"REFERENCE_DIR      : {REFERENCE_DIR.absolute()}")
    log.info(f"SIMILARITY_THRESHOLD: {SIMILARITY_THRESHOLD:.2f} ({SIMILARITY_THRESHOLD*100:.0f}%)")
    log.info(f"SCAN_INTERVAL      : {SCAN_INTERVAL}s")

    seen = load_seen()
    ref_hashes = load_ref_hashes()

    send_telegram(
        f"✅ <b>Bot démarré !</b>\n"
        f"📸 <b>{len(ref_hashes)}</b> images de référence\n"
        f"🔍 <b>{len(KEYWORDS)}</b> mots-clés surveillés\n"
        f"📊 Seuil : <b>{SIMILARITY_THRESHOLD * 100:.0f}%</b>\n"
        f"👥 <b>{len(CHAT_IDS)}</b> destinataire(s)\n"
        f"⏱ Scan toutes les <b>{SCAN_INTERVAL // 60}min{SCAN_INTERVAL % 60:02d}s</b>"
    )

    scan_count = 0
    while True:
        scan_count += 1
        log.info(f"─── Scan #{scan_count} · {datetime.now().strftime('%d/%m %H:%M:%S')} ───")

        ref_hashes = load_ref_hashes()
        new_matches = 0
        seen_this   = set()

        for keyword in KEYWORDS:
            items = fetch_items(keyword)
            time.sleep(3)  # pause polie entre chaque mot-clé

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
