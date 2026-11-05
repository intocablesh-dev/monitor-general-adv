#!/usr/bin/env python3
"""Notificador programado. La configuración se lee del secreto CONFIG."""

import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import base64
import hashlib
import hmac
from datetime import datetime, timezone
import re

CONFIG = json.loads(os.environ.get("CONFIG") or "{}")
STORES = CONFIG.get("stores", [])
KEYWORDS = CONFIG.get("keywords", [])
T = CONFIG.get("texts", {})
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = ",".join(str(c) for c in CONFIG.get("chat_ids", []))
TEST_MESSAGE = os.environ.get("TEST_MESSAGE", "0") in ("1", "true", "True")
DATA_FILE = os.path.join(os.environ.get("DATA_DIR", ".data"), "d.bin")
LEGACY_STATE = "state.json"
ONLY_STORE_URL = os.environ.get("STORE_URL", "")
CHAT_IDS = []
BASELINE = False  # True si no hay estado guardado (evita repetir avisos ya enviados)
FAIL_ALERT_AFTER = 3  # avisos técnicos tras N fallos seguidos de una fuente
DOWN_ALERT_AFTER = int(os.environ.get("DOWN_ALERT_AFTER", "3"))  # caída: N revisiones seguidas (~N minutos)
ALERT_REPEAT = int(os.environ.get("ALERT_REPEAT", "3"))   # repeticiones de la alerta de disponible
ALERT_GAP_SECONDS = int(os.environ.get("ALERT_GAP_SECONDS", "60"))  # espera entre repeticiones
HEARTBEAT_HOUR_UTC = 13  # 8:00 a. m. en Colombia

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CO,es;q=0.9",
}


def log(*args, **kwargs):
    """Sin detalles en los registros públicos (activar con DEBUG=1 en pruebas locales)."""
    if os.environ.get("DEBUG") == "1":
        print(*args, **kwargs)


def norm(text):
    """minúsculas y sin tildes, para comparar nombres."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def matches(name):
    return any(norm(k) in norm(name) for k in KEYWORDS)


def http_get_json(url, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url} -> {last_err}")


def page_status(url):
    """Devuelve None si la página principal responde; si no, una descripción corta del problema.
    Solo cuenta como caída: error 5xx, sin conexión o sin respuesta (403/429 = bloqueo, no caída)."""
    problem = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={**HEADERS, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                resp.read(2048)
            return None
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return None
            problem = f"error {e.code}"
        except Exception as e:  # noqa: BLE001
            text = str(e).lower()
            problem = "no responde" if "timed out" in text or "timeout" in text else "sin conexión"
        if attempt == 0:
            time.sleep(5)
    return problem


def to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------- Fuente tipo A ----------

def vtex_info(base, p):
    name = p.get("productName") or p.get("productTitle") or "(sin nombre)"
    link = p.get("link") or ""
    if not link.startswith("http"):
        slug = p.get("linkText")
        link = f"{base}{link}" if link.startswith("/") else (f"{base}/{slug}/p" if slug else base)
    available, price = False, None
    for item in p.get("items") or []:
        for seller in item.get("sellers") or []:
            offer = seller.get("commertialOffer") or {}
            if (offer.get("AvailableQuantity") or 0) > 0 or offer.get("IsAvailable"):
                available = True
            pr = to_number(offer.get("Price"))
            if pr and (price is None or pr < price):
                price = pr
    pid = str(p.get("productId") or p.get("productReference") or link)
    return pid, {"name": name, "url": link, "available": available, "price": price}


def find_vtex(base):
    found, errors, ok = {}, [], 0
    for term in KEYWORDS:
        q = urllib.parse.quote(term)
        urls = [
            (f"{base}/api/catalog_system/pub/products/search?ft={q}&_from=0&_to=49", None),
            (f"{base}/api/io/_v/api/intelligent-search/product_search/"
             f"?query={q}&count=50&hideUnavailableItems=false", "products"),
        ]
        for url, key in urls:
            try:
                data = http_get_json(url)
                results = data.get(key) if key else data
                if not isinstance(results, list):
                    raise RuntimeError("respuesta inesperada")
                ok += 1
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))
                continue
            for p in results:
                pid, info = vtex_info(base, p)
                if matches(info["name"]):
                    prev = found.get(pid)
                    if not prev or (info["available"] and not prev["available"]):
                        found[pid] = info
    if ok == 0:
        raise RuntimeError("; ".join(errors))
    return found


# ---------- Fuente tipo B ----------

def shopify_info(base, p):
    variants = p.get("variants") or []
    available = any(v.get("available") for v in variants) or bool(p.get("available"))
    prices = [to_number(v.get("price")) for v in variants] + [to_number(p.get("price"))]
    prices = [x for x in prices if x]
    price = min(prices) if prices else None
    # search/suggest devuelve precios en pesos; products.json también ("150000.00")
    url = p.get("url") or f"/products/{p.get('handle', '')}"
    if not url.startswith("http"):
        url = base + url.split("?")[0]
    info = {"name": p.get("title") or "(sin nombre)", "url": url,
            "available": available, "price": price}
    if "body_html" in p:  # viene de products.json: guardamos la ficha para detectar cambios
        tags = p.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        info["fp"] = {
            "tags": sorted(tags),
            "price": price,
            "type": p.get("product_type") or "",
            "body": " ".join(re.sub(r"<[^>]+>", " ", p.get("body_html") or "").split()),
        }
    return str(p.get("id") or p.get("handle")), info


def find_shopify(base):
    found, errors, ok = {}, [], 0
    # 1) Catálogo completo
    try:
        for page in range(1, 21):
            data = http_get_json(f"{base}/products.json?limit=250&page={page}")
            products = data.get("products")
            if not isinstance(products, list):
                raise RuntimeError("respuesta inesperada de products.json")
            ok += 1
            for p in products:
                pid, info = shopify_info(base, p)
                if matches(info["name"]):
                    found[pid] = info
            if len(products) < 250:
                break
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
    # 2) Buscador (respaldo)
    for term in KEYWORDS:
        q = urllib.parse.quote(term)
        url = (f"{base}/search/suggest.json?q={q}"
               f"&resources[type]=product&resources[limit]=10&resources[options][unavailable_products]=show")
        try:
            data = http_get_json(url)
            products = (((data.get("resources") or {}).get("results") or {}).get("products"))
            if not isinstance(products, list):
                raise RuntimeError("respuesta inesperada de search/suggest")
            ok += 1
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
            continue
        for p in products:
            pid, info = shopify_info(base, p)
            if matches(info["name"]):
                prev = found.get(pid)
                if not prev:
                    found[pid] = info
                elif info["available"] and not prev["available"]:
                    prev["available"] = True
    if ok == 0:
        raise RuntimeError("; ".join(errors))
    return found


def find_products(store):
    base = (ONLY_STORE_URL or store["url"]).rstrip("/")
    return find_vtex(base) if store["type"] == "vtex" else find_shopify(base)


# ---------- Telegram ----------

def discover_chats():
    """Devuelve {chat_id: tipo} de los chats (privados o grupos) que le escribieron al bot."""
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        log("No se pudo leer getUpdates:", e, file=sys.stderr)
        return {}
    chats = {}
    for upd in data.get("result", []):
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            msg = upd.get(key) or {}
            chat = msg.get("chat")
            if chat and chat.get("id"):
                chats[str(chat["id"])] = chat.get("type")
            if msg.get("migrate_to_chat_id"):  # grupo convertido en supergrupo
                chats[str(msg["migrate_to_chat_id"])] = "supergroup"
    return chats


def post_message(chat_id, text):
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def send_telegram(text):
    if not TOKEN or not CHAT_IDS:
        log("[sin Telegram configurado]\n" + text)
        return
    for chat_id in list(CHAT_IDS):
        try:
            post_message(chat_id, text)
            log(f"Enviado a un chat ({len(text)} caracteres)")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")
            log("Error de Telegram:", e.code, detail, file=sys.stderr)
            try:
                new_id = json.loads(detail).get("parameters", {}).get("migrate_to_chat_id")
            except json.JSONDecodeError:
                new_id = None
            if new_id:
                CHAT_IDS.remove(chat_id)
                CHAT_IDS.append(str(new_id))
                try:
                    post_message(str(new_id), text)
                except Exception as e2:  # noqa: BLE001
                    log("Error reintentando:", e2, file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            log("Error enviando:", e, file=sys.stderr)


def urgent(title, body):
    """Alerta importante: se repite ALERT_REPEAT veces con una pausa entre cada una."""
    for n in range(1, ALERT_REPEAT + 1):
        send_telegram(f"{title} ({n}/{ALERT_REPEAT})\n\n{body}")
        if n < ALERT_REPEAT:
            time.sleep(ALERT_GAP_SECONDS)


def fmt_price(price):
    return f"${price:,.0f}".replace(",", ".") if price else T["no_price"]


# ---------- Estado ----------

def _key():
    return hashlib.sha256(b"data-v1" + TOKEN.encode()).digest()


def _stream(key, nonce, n):
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + i.to_bytes(8, "big")).digest()
        i += 1
    return out[:n]


def load_state():
    """Lee el estado cifrado; si no existe, intenta el formato anterior (state.json)."""
    try:
        raw = base64.b64decode(open(DATA_FILE, "rb").read())
        nonce, mac, ct = raw[:16], raw[16:48], raw[48:]
        key = _key()
        if not hmac.compare_digest(mac, hmac.new(key, nonce + ct, hashlib.sha256).digest()):
            raise ValueError("estado inválido")
        pt = bytes(x ^ y for x, y in zip(ct, _stream(key, nonce, len(ct))))
        return json.loads(pt.decode("utf-8"))
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(LEGACY_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(state):
    state["chat_ids"] = CHAT_IDS
    pt = json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
    key, nonce = _key(), os.urandom(16)
    ct = bytes(x ^ y for x, y in zip(pt, _stream(key, nonce, len(pt))))
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "wb") as f:
        f.write(base64.b64encode(nonce + mac + ct))
    return pt


def setup_chats(state):
    global CHAT_IDS
    saved = state.get("chat_ids") or ([state["chat_id"]] if state.get("chat_id") else [])
    state.pop("chat_id", None)
    CHAT_IDS = list(dict.fromkeys([c.strip() for c in CHAT_ID.split(",") if c.strip()] + saved))
    # Solo en la prueba manual se registran chats nuevos (así nadie más se cuela).
    if TOKEN and (TEST_MESSAGE or not CHAT_IDS):
        for cid in discover_chats():
            if cid not in CHAT_IDS:
                CHAT_IDS.append(cid)
        log("Chats registrados:", len(CHAT_IDS))
    if not CHAT_IDS:
        log("Aún no conozco ningún chat: escríbele al bot.", file=sys.stderr)


# ---------- Principal ----------

def check_store(store, state):
    """Revisa una fuente, envía avisos y devuelve una línea de resumen."""
    key, name = store["key"], store["name"]
    fails = state.setdefault("fails", {})
    known = state.setdefault("products", {})
    down = state.setdefault("down", {})  # {fuente: {"n": fallos seguidos, "since": epoch}}

    # ¿La página principal está caída?
    problem = page_status((ONLY_STORE_URL or store["url"]).rstrip("/") + "/")
    d = down.get(key) or {"n": 0, "since": None}
    if problem:
        d["n"] += 1
        d["since"] = d["since"] or time.time()
        down[key] = d
        mins = max(1, round((time.time() - d["since"]) / 60))
        if d["n"] == DOWN_ALERT_AFTER:
            send_telegram(T.get("down", "🔴 La página de {name} parece estar caída: lleva unos {mins} min "
                                "sin funcionar ({detail}). Te aviso cuando vuelva.")
                          .format(name=name, mins=mins, detail=problem))
        return T.get("sum_down", "• {name}: 🔴 página caída ({detail})").format(name=name, detail=problem)
    if d["n"] >= DOWN_ALERT_AFTER:
        mins = max(1, round((time.time() - d["since"]) / 60))
        send_telegram(T.get("up", "🟢 La página de {name} volvió a funcionar (estuvo caída unos {mins} min).")
                      .format(name=name, mins=mins))
    down[key] = {"n": 0, "since": None}

    try:
        current = find_products(store)
    except Exception as e:  # noqa: BLE001
        fails[key] = fails.get(key, 0) + 1
        log(f"[{name}] No se pudo consultar:", e, file=sys.stderr)
        if fails[key] == FAIL_ALERT_AFTER:
            send_telegram(T["fail"].format(name=name, n=fails[key], detail=str(e)[:400]))
        return T["sum_fail"].format(name=name, detail=str(e)[:120])

    if fails.get(key, 0) >= FAIL_ALERT_AFTER:
        send_telegram(T["back"].format(name=name))
    fails[key] = 0

    early_until = store.get("early_until")
    in_early = bool(early_until) and datetime.now(timezone.utc) <= datetime.fromisoformat(early_until)

    for pid, info in current.items():
        skey = f"{key}:{pid}"
        before = known.get(skey)
        line = f"{info['name']}\n{fmt_price(info['price'])}\n{info['url']}"
        if info["available"] and (not before or not before.get("available")):
            if in_early:
                send_telegram(T["early"].format(name=name, line=line))
            else:
                extra = T["late_extra"] if early_until else ""
                urgent(T["avail_title"].format(NAME=name.upper(), extra=extra),
                       T["avail_body"].format(line=line))
        elif not before and not BASELINE:
            send_telegram(T["appeared"].format(name=name, line=line))
        elif before and before.get("available") and not info["available"]:
            send_telegram(T["gone"].format(name=name, line=line))

        # cambios en la ficha (etiquetas, precio, tipo, descripción)
        fp_old, fp_new = (before or {}).get("fp"), info.get("fp")
        if fp_old and fp_new and fp_old != fp_new and not BASELINE:
            cambios = []
            added = set(fp_new["tags"]) - set(fp_old["tags"])
            removed = set(fp_old["tags"]) - set(fp_new["tags"])
            if added or removed:
                cambios.append(T["c_tags"] + " ".join([f"+{t}" for t in sorted(added)] +
                                                       [f"−{t}" for t in sorted(removed)]))
            if fp_new["price"] != fp_old["price"]:
                cambios.append(T["c_price"] + f"{fmt_price(fp_old['price'])} → {fmt_price(fp_new['price'])}")
            if fp_new["type"] != fp_old["type"]:
                cambios.append(T["c_type"] + f"{fp_old['type']} → {fp_new['type']}")
            if fp_new["body"] != fp_old["body"]:
                cambios.append(T["c_body"] + fp_new["body"][:700])
            texto = "\n".join(cambios)
            if any(norm(w) in norm(texto) for w in CONFIG.get("urgent_words", [])):
                urgent(T["edit_urgent"].format(name=name), f"{line}\n\n{texto}")
            else:
                send_telegram(T["edit"].format(name=name, line=line, changes=texto))

        entry = {"name": info["name"], "url": info["url"], "available": info["available"]}
        if info.get("fp") or (before or {}).get("fp"):
            entry["fp"] = info.get("fp") or before.get("fp")
        known[skey] = entry

    if not current:
        return T["sum_none"].format(name=name)
    return "\n".join(T["sum_item"].format(name=name, item=i["name"],
                                          status=T["st_yes"] if i["available"] else T["st_no"])
                     for i in current.values())


def main():
    global BASELINE
    had_data = os.path.exists(DATA_FILE)
    state = load_state()
    BASELINE = not state.get("products") and not state.get("fails")
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)
    state.setdefault("products", {})

    setup_chats(state)

    summary = [check_store(s, state) for s in STORES]
    resumen = "\n".join(summary)
    log(resumen)

    today = time.strftime("%Y-%m-%d", time.gmtime())
    if time.gmtime().tm_hour >= HEARTBEAT_HOUR_UTC and state.get("last_heartbeat") != today:
        send_telegram(T["daily"].format(summary=resumen))
        state["last_heartbeat"] = today

    if TEST_MESSAGE:
        send_telegram(T["test"].format(keywords=", ".join(KEYWORDS), summary=resumen))

    state["chat_ids"] = CHAT_IDS
    changed = (not had_data) or json.dumps(state, ensure_ascii=False, sort_keys=True) != before
    save_state(state)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
    print("ok")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(e)
        print("error")
        sys.exit(1)
