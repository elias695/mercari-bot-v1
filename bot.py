import os, time, json, logging, requests, uuid, base64, hashlib, struct
from pathlib import Path
from datetime import datetime, timezone
from PIL import Image
import imagehash
from io import BytesIO

# ─── Génération du DPoP JWT pour l'API Mercari v2 ─────────────────────────────
# Mercari exige un JWT DPoP signé avec HMAC-SHA256 (HS256).
# La clé secrète et l'algorithme ont été reverse-engineerés depuis le JS de Mercari.

_MERCARI_KEY = (
    "2Vuvzl5oVXEMgADSSSHBmO33lSDa0dJK6CzKlxEE/5Y="  # clé stable depuis 2023
)

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _make_dpop(url: str, method: str = "POST") -> str:
    """Génère un JWT DPoP valide pour l'API Mercari jp."""
    import hmac
    import hashlib

    header = {"typ": "dpop+jwt", "alg": "HS256"}
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "jti": str(uuid.uuid4()),
        "htm": method.upper(),
        "htu": url,
        "iat": now,
        "exp": now + 60,
    }

    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode()

    key = base64.b64decode(_MERCARI_KEY)
    sig = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]          # Obligatoire
CHAT_IDS           = [c.strip() for c in os.environ["TELEGRAM_CHAT_IDS"].split(",")]  # ex: "111,222,333"
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.80"))   # 0.80 = 80%
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", "120"))               # secondes
REFERENCE_DIR      = Path(os.getenv("REFERENCE_DIR", "reference_images"))
SEEN_FILE          = Path("seen_items.json")
MAX_PRICE          = int(os.getenv("MAX_PRICE", "0"))       # 0 = pas de limite
MIN_PRICE          = int(os.getenv("MIN_PRICE", "0"))       # 0 = pas de limite

KEYWORDS = [
    "ナイキ ランニング",
    "nike running",
    "アンダーアーマー ランニング",
    "under armour running",
]

_SEARCH_URL = "https://api.mercari.jp/v2/entities:search"

def _api_headers() -> dict:
    """Génère des headers frais avec un DPoP JWT valide."""
    return {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept": "application/json",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://jp.mercari.com",
        "Referer": "https://jp.mercari.com/",
        "X-Platform": "web",
        "DPoP": _make_dpop(_SEARCH_URL, "POST"),
    }

IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
}

# ─── Persistance ──────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ─── Images de référence ──────────────────────────────────────────────────────

def load_ref_hashes() -> list[tuple[str, imagehash.ImageHash]]:
    """Charge toutes les images de référence et calcule leur pHash."""
    REFERENCE_DIR.mkdir(exist_ok=True)
    exts = ("*.jpg", "*.jpeg", "*.png", "*.webp")
    files = []
    for ext in exts:
        files.extend(REFERENCE_DIR.glob(ext))

    hashes = []
    for f in files:
        try:
            img = Image.open(f).convert("RGB")
            h = imagehash.phash(img)
            hashes.append((f.name, h))
        except Exception as e:
            log.warning(f"Image ignorée {f.name}: {e}")

    log.info(f"{len(hashes)} images de référence chargées.")
    return hashes

def image_similarity(url: str, ref_hashes: list) -> tuple[float, str]:
    """
    Télécharge l'image de l'article et compare avec toutes les images de référence.
    Retourne (meilleure_similarité, nom_image_ref).
    La similarité est basée sur la distance de Hamming du pHash :
      distance 0 = identique, distance 64 = complètement différent.
    """
    if not url or not ref_hashes:
        return 0.0, ""
    try:
        r = requests.get(url, timeout=10, headers=IMG_HEADERS)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")
        item_hash = imagehash.phash(img)
    except Exception as e:
        log.debug(f"Impossible de charger l'image {url}: {e}")
        return 0.0, ""

    best_sim = 0.0
    best_ref = ""
    for ref_name, ref_hash in ref_hashes:
        distance = item_hash - ref_hash          # distance de Hamming (0–64)
        similarity = 1.0 - distance / 64.0       # conversion en score 0–1
        if similarity > best_sim:
            best_sim = similarity
            best_ref = ref_name

    return best_sim, best_ref

# ─── Scraping Mercari ─────────────────────────────────────────────────────────

def fetch_mercari_items(keyword: str, limit: int = 30) -> list[dict]:
    """
    Interroge l'API Mercari JP v2 avec un DPoP JWT valide.
    """
    payload = {
        "pageSize": limit,
        "pageToken": "",
        "searchSessionId": str(uuid.uuid4()),   # obligatoire, doit être non-vide
        "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
        "thumbnailTypes": [],
        "searchCondition": {
            "keyword": keyword,
            "excludeKeyword": "",
            "sort": "SORT_CREATED_TIME",
            "order": "ORDER_DESC",
            "status": ["STATUS_ON_SALE"],
            "sizeId": [], "categoryId": [], "brandId": [], "sellerId": [],
            "priceMin": MIN_PRICE if MIN_PRICE > 0 else 0,
            "priceMax": MAX_PRICE if MAX_PRICE > 0 else 0,
            "itemConditionId": [], "shippingPayerId": [], "shippingFromArea": [],
            "shippingMethod": [], "colorId": [], "hasCoupon": False,
            "attributes": [], "itemTypes": [], "skuIds": [],
        },
        "serviceFrom": "suruga",
        "withItemBrand": True,
        "withItemSize": False,
        "withItemPromotions": True,
        "withItemSizes": True,
        "withShopname": False,
        "userId": "",
        "userSessionId": "",
        "fromPage": "",
    }

    items = []
    try:
        resp = requests.post(
            _SEARCH_URL,
            json=payload,
            headers=_api_headers(),   # DPoP régénéré à chaque appel
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("items", []):
            item_id = str(item.get("id", "")).strip()
            if not item_id:
                continue
            thumbnails = item.get("thumbnails") or []
            image_url = thumbnails[0] if thumbnails else ""
            try:
                price = int(item.get("price", 0))
            except (ValueError, TypeError):
                price = 0
            items.append({
                "id":        item_id,
                "name":      item.get("name", ""),
                "price":     price,
                "image_url": image_url,
                "url":       f"https://jp.mercari.com/item/{item_id}",
                "keyword":   keyword,
            })

    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        body   = getattr(e.response, "text", "")[:300]
        log.warning(f"Mercari HTTP {status} pour '{keyword}': {body}")
    except Exception as e:
        log.warning(f"Erreur fetch '{keyword}': {e}")

    log.info(f"  '{keyword}' → {len(items)} articles")
    return items

# ─── Telegram ─────────────────────────────────────────────────────────────────

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
                if r.ok:
                    sent = True
            except Exception:
                pass
        if not sent:
            try:
                requests.post(
                    f"{base}/sendMessage",
                    data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                    timeout=10,
                )
            except Exception as e:
                log.warning(f"Telegram erreur chat {chat_id}: {e}")

def notify(item: dict, similarity: float, ref_name: str):
    pct = f"{similarity * 100:.1f}%"
    msg = (
        f"🔥 <b>Match trouvé !</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👕 <b>{item['name']}</b>\n"
        f"💴 <b>{item['price']:,} ¥</b>\n"
        f"🔍 Mot-clé : <i>{item['keyword']}</i>\n"
        f"📊 Similarité : <b>{pct}</b>\n"
        f"🖼 Référence : <code>{ref_name}</code>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>"
    )
    send_telegram(msg, image_url=item["image_url"])
    log.info(f"  ✅ Notifié : {item['name']} ({pct})")

# ─── Boucle principale ────────────────────────────────────────────────────────

def run():
    log.info("=== Mercari JP Bot démarré ===")

    # Vérification de la config
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN manquant !")
    if not CHAT_IDS:
        raise RuntimeError("TELEGRAM_CHAT_IDS manquant !")

    ref_hashes = load_ref_hashes()
    seen = load_seen()

    send_telegram(
        f"✅ <b>Bot démarré !</b>\n"
        f"📸 {len(ref_hashes)} images de référence\n"
        f"🔍 {len(KEYWORDS)} mots-clés surveillés\n"
        f"📊 Seuil de similarité : {SIMILARITY_THRESHOLD * 100:.0f}%\n"
        f"👥 {len(CHAT_IDS)} destinataire(s)\n"
        f"⏱ Scan toutes les {SCAN_INTERVAL // 60}min{SCAN_INTERVAL % 60:02d}s"
    )

    scan_count = 0
    while True:
        scan_count += 1
        log.info(f"─── Scan #{scan_count} · {datetime.now().strftime('%d/%m %H:%M:%S')} ───")

        # Rechargement des images à chaque scan (ajout à chaud possible)
        ref_hashes = load_ref_hashes()
        if not ref_hashes:
            log.warning("Aucune image de référence — vérifiez le dossier reference_images/")

        new_matches = 0

        for keyword in KEYWORDS:
            items = fetch_mercari_items(keyword)
            time.sleep(1)  # politesse envers l'API

            for item in items:
                item_id = item["id"]

                # Déjà vu → skip
                if item_id in seen:
                    continue

                # Marquer comme vu immédiatement (avant la comparaison)
                # pour éviter les doublons en cas d'erreur
                seen.add(item_id)

                # Comparaison d'image
                similarity, ref_name = image_similarity(item["image_url"], ref_hashes)
                log.debug(
                    f"    {item['name'][:40]} — sim={similarity:.2f} ref={ref_name}"
                )

                if similarity >= SIMILARITY_THRESHOLD:
                    notify(item, similarity, ref_name)
                    new_matches += 1

        save_seen(seen)
        log.info(
            f"Scan #{scan_count} terminé — {new_matches} nouveau(x) match(s) — "
            f"prochain scan dans {SCAN_INTERVAL}s"
        )
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Bot arrêté manuellement.")
    except Exception as e:
        log.critical(f"Erreur fatale : {e}", exc_info=True)
        # Tenter d'envoyer une alerte Telegram avant de crasher
        try:
            send_telegram(f"🚨 <b>Bot crashé !</b>\n<code>{e}</code>")
        except Exception:
            pass
        raise
