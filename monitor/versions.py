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
    # saytning umumiy (har bir maqolada bir xil) rasmi — solishtirishga yaramaydi
    if re.search(r"(logo|default|placeholder|og[-_]?image|share|no[-_]?image|noimage|cover|banner|favicon)", name):
        return ""
    return name if len(name) >= 6 else ""


def outlet(rec: dict) -> str:
    """Nashr: sayt domeni yoki (aniqlangan bo'lsa) Telegram kanali egasi bo'lgan sayt."""
    return rec.get("outlet") or site_key(rec["url"])


def _close(a, b, hours):
    import datetime as _dt
    try:
        da = _dt.datetime.fromisoformat(a.get("date") or a.get("found_at"))
        db = _dt.datetime.fromisoformat(b.get("date") or b.get("found_at"))
    except (TypeError, ValueError):
        return False
    return abs((da - db).total_seconds()) <= hours * 3600


def _url_id(u):
    m = re.search(r"[-/_](\d{3,})(?:\.html?|/)?$", up.urlsplit(u).path)
    return m.group(1) if m else None


def same_story(a: dict, b: dict) -> bool:
    """Bitta nashrdagi bitta xabar (boshqa til yoki sayt + o'z Telegram kanali)."""
    if outlet(a) != outlet(b):
        return False
    # Telegram posti aynan shu nashrning shu maqolasiga havola bersa.
    # Faqat nashrning o'z saytidagi maqola havolalari hisoblanadi: qo'mita kanaliga, stat.uz'ga
    # yoki bosh sahifaga havolalar (ko'p xabarda bir xil bo'ladi) hisobga olinmaydi.
    own = outlet(a)
    def own_links(r):
        out = set()
        for x in (r.get("links") or []) + [r["url"]]:
            sp = up.urlsplit(x)
            if site_key(x) == own and len(sp.path.strip("/")) > 8:
                out.add(site_key(x) + sp.path.rstrip("/"))
        return out
    if own_links(a) & own_links(b):
        return True
    # bir xil maqola raqami (masalan, ...-5252 va oz/...-5252)
    ia, ib = _url_id(a["url"]), _url_id(b["url"])
    if ia and ia == ib and a["url"] != b["url"] and "t.me" not in a["url"]:
        return True
    # Bir xil rasm/raqamlar — faqat bitta nashr ichida (boshqa nashrlar qo'mita rasmini
    # va raqamlarini takrorlagani bilan ular birlashtirilmaydi — yuqorida outlet tekshirildi).
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
    # Boshqa til versiyasi: tili farq qiladi, 3 soat ichida chiqqan va kamida 2 ta raqam bir xil
    if a.get("lang") != b.get("lang") and len(common) >= 2 and _close(a, b, 3):
        return True
    if len(common) >= 2 and small <= 3 and ratio >= 0.66:
        return True
    return False
