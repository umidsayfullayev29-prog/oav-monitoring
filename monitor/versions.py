"""Bitta saytdagi bir maqolaning turli tildagi versiyalarini aniqlash.

Masalan gazeta.uz/oz/..., gazeta.uz/uz/... va gazeta.uz/ru/... — bitta xabar.
Belgilar: bir xil asosiy rasm (og:image) yoki matn boshidagi raqamlar to'plami deyarli bir xil.
"""
import re
import urllib.parse as up

LANG_PATH = re.compile(r"^/(uz|oz|ru|en|kr|ky|kk|tj|tr|uzc|uzl|cyr|lat)(/|$)", re.I)
NUM = re.compile(r"\d+(?:[.,]\d+)?")


def site_key(url: str) -> str:
    """Til subdomenisiz asosiy domen: ru.sputniknews.uz -> sputniknews.uz."""
    sp = up.urlsplit(url)
    h = (sp.hostname or "").lower()
    if h.startswith("www."):
        h = h[4:]
    if h == "t.me":
        return "t.me/" + (sp.path.strip("/").split("/") or [""])[0].lower()
    parts = h.split(".")
    if len(parts) > 2 and parts[0] in ("uz", "oz", "ru", "en", "kr", "m", "rus", "eng", "uzb", "cyr", "lat"):
        h = ".".join(parts[1:])
    return h


def lang_label(url: str, title: str = "") -> str:
    p = up.urlsplit(url)
    m = LANG_PATH.match(p.path or "")
    if m:
        return {"oz": "ўз", "uzc": "ўз", "cyr": "ўз", "kr": "ўз", "uzl": "uz", "lat": "uz"}.get(m.group(1).lower(), m.group(1).lower())
    sub = (p.hostname or "").split(".")[0].lower()
    if sub in ("ru", "rus"):
        return "ru"
    if sub in ("en", "eng"):
        return "en"
    if sub in ("oz", "kr", "cyr"):
        return "ўз"
    t = title or ""
    cyr = len(re.findall(r"[а-яёА-ЯЁ]", t))
    uzc = len(re.findall(r"[ўқғҳЎҚҒҲ]", t))
    lat = len(re.findall(r"[a-zA-Z]", t))
    if uzc:
        return "ўз"
    if cyr > lat:
        return "ru"
    if re.search(r"[ʻʼ‘’']|o‘|g‘|\bva\b|\bbo'yicha|\bmlrd|\bming\b", t, re.I):
        return "uz"
    return "en" if lat else "?"


def numbers(text: str):
    """Matndagi muhim raqamlar (yil va 1 xonalilar emas)."""
    out = set()
    for m in NUM.finditer(text or ""):
        n = m.group(0).replace(",", ".")
        if len(n.replace(".", "")) < 2:
            continue
        if re.fullmatch(r"(19|20)\d\d", n):
            continue
        out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return sorted(out)


def image_key(url: str) -> str:
    if not url:
        return ""
    p = up.urlsplit(url).path
    name = p.rsplit("/", 1)[-1].lower()
    name = re.sub(r"[-_]?\d{2,4}x\d{2,4}", "", name)      # o'lcham qo'shimchasi
    name = re.sub(r"\.(jpe?g|png|webp|gif)$", "", name)
    return name if len(name) >= 6 else ""


def outlet(rec: dict) -> str:
    """Nashr: sayt domeni yoki (aniqlangan bo'lsa) Telegram kanali egasi bo'lgan sayt."""
    return rec.get("outlet") or site_key(rec["url"])


def same_story(a: dict, b: dict) -> bool:
    """Bitta nashrdagi bitta xabar (boshqa til yoki sayt + o'z Telegram kanali)."""
    if outlet(a) != outlet(b):
        return False
    # Telegram posti aynan shu maqolaga havola bersa
    na_url = {x.rstrip("/") for x in (a.get("links") or [])}
    nb_url = {x.rstrip("/") for x in (b.get("links") or [])}
    if a["url"].rstrip("/") in nb_url or b["url"].rstrip("/") in na_url or (na_url & nb_url):
        return True
    if a.get("img") and a.get("img") == b.get("img"):
        return True
    na, nb = set(a.get("nums") or []), set(b.get("nums") or [])
    common = na & nb
    small = min(len(na), len(nb))
    if small == 0:
        return False
    ratio = len(common) / small
    if len(common) >= 3 and ratio >= 0.5:
        return True
    if len(common) >= 2 and small <= 3 and ratio >= 0.66:
        return True
    return False
