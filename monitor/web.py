"""HTTP so'rovlar, maqola matnini ajratish, havolalarni yig'ish."""
import re
import json
import threading
import datetime as dt
import urllib.parse as up
from email.utils import parsedate_to_datetime

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uz,ru;q=0.9,en;q=0.8",
}
MAX_BYTES = 4_000_000

_session = None


def session():
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=0)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _session = s
    return _session


_dom_locks = {}
_dom_guard = threading.Lock()


def _dom_sem(url):
    host = host_of(url)
    with _dom_guard:
        if host not in _dom_locks:
            # bitta saytga bir vaqtda 4 tadan ortiq so'rov emas (t.me — 8)
            _dom_locks[host] = threading.Semaphore(8 if host == "t.me" else 4)
        return _dom_locks[host]


def fetch(url: str, timeout=(7, 15)):
    """(final_url, text) yoki istisno. SSL xato bo'lsa sertifikatsiz qayta urinadi."""
    with _dom_sem(url):
        return _fetch(url, timeout)


def _fetch(url, timeout):
    if isinstance(timeout, (int, float)):
        timeout = (min(7, timeout), timeout)
    s = session()
    try:
        r = s.get(url, timeout=timeout, allow_redirects=True, stream=True)
    except requests.exceptions.SSLError:
        r = s.get(url, timeout=timeout, allow_redirects=True, stream=True, verify=False)
    try:
        r.raise_for_status()
        chunks, size = [], 0
        for chunk in r.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                break
        raw = b"".join(chunks)
    finally:
        r.close()
    enc = r.encoding
    if not enc or enc.lower() == "iso-8859-1":
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:3000])
        enc = m.group(1).decode() if m else "utf-8"
    try:
        text = raw.decode(enc, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return r.url, text


# ---------------------------------------------------------------- URL
TRACKING = re.compile(r"^(utm_|fbclid|gclid|yclid|_ga|ref$|from$|share)")


def norm_url(url: str) -> str:
    try:
        p = up.urlsplit(url.strip())
    except ValueError:
        return url
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if p.port and p.port not in (80, 443):
        host = f"{host}:{p.port}"
    q = [(k, v) for k, v in up.parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
    path = re.sub(r"/+$", "", p.path) or "/"
    return up.urlunsplit(("https", host, path, up.urlencode(q), ""))


def host_of(url: str) -> str:
    h = (up.urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def same_site(url: str, domain: str) -> bool:
    h = host_of(url)
    return h == domain or h.endswith("." + domain)


# ---------------------------------------------------------------- listing
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|docx?|xlsx?|zip|rar|mp[34]|avi|mov)$", re.I)
SKIP_PATH = re.compile(
    r"/(tag|tags|category|categories|rubric|rubrika|author|authors|search|page|login|register|"
    r"about|contact|contacts|advert|reklama|privacy|policy|terms|rss|feed|archive|arxiv|"
    r"video-category|photo-category|subscribe|user|profile|amp)(/|$)", re.I)


def looks_like_article(url: str) -> bool:
    p = up.urlsplit(url)
    path = p.path
    if SKIP_EXT.search(path) or SKIP_PATH.search(path):
        return False
    segs = [s for s in path.split("/") if s]
    if not segs:
        return False
    last = segs[-1]
    if re.search(r"\d{3,}", path):
        return True
    if len(segs) >= 2 and (len(last) >= 25 or last.count("-") >= 3):
        return True
    if len(segs) == 1 and (len(last) >= 30 and last.count("-") >= 3):
        return True
    return False


def extract_links(html: str, base_url: str, domain: str):
    """Sahifadagi maqola havolalari: [(url, anchor_text)], va RSS havolalari."""
    soup = BeautifulSoup(html, "lxml")
    feeds = []
    for l in soup.find_all("link", href=True):
        t = (l.get("type") or "").lower()
        if "rss" in t or "atom" in t:
            feeds.append(up.urljoin(base_url, l["href"]))
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = up.urljoin(base_url, href)
        if not url.startswith("http") or not same_site(url, domain):
            continue
        if not looks_like_article(url):
            continue
        n = norm_url(url)
        if n in seen:
            continue
        seen.add(n)
        links.append((url, " ".join(a.get_text(" ", strip=True).split())[:300]))
    return links, feeds


# ---------------------------------------------------------------- article
NOISE = re.compile(r"(related|comment|sidebar|footer|header|menu|share|social|popular|banner|"
                   r"advert|reklama|subscribe|breadcrumb|tags|navbar|widget|recommend|read-also|"
                   r"readmore|most-read|trending|cookie|modal|similar|latest-news|news-list)", re.I)


def _meta(soup, *names):
    for n in names:
        el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
            or soup.find("meta", attrs={"itemprop": n})
        if el and el.get("content"):
            return el["content"].strip()
    return ""


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError):
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
            if not m:
                m2 = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s)
                if not m2:
                    return None
                d = dt.datetime(int(m2.group(3)), int(m2.group(2)), int(m2.group(1)))
            else:
                d = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone(dt.timedelta(hours=5)))
    return d


def _ld_json(soup):
    out = {}
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            if str(it.get("@type", "")).lower().endswith(("article", "newsarticle", "reportage", "blogposting")) \
                    or "articleBody" in it:
                out.setdefault("title", it.get("headline"))
                out.setdefault("date", it.get("datePublished"))
                if it.get("articleBody"):
                    out["body"] = it["articleBody"]
    return out


def extract_article(html: str):
    soup = BeautifulSoup(html, "lxml")
    ld = _ld_json(soup)
    title = _meta(soup, "og:title", "twitter:title") or ld.get("title") or ""
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "")
    desc = _meta(soup, "og:description", "description", "twitter:description")
    image = _meta(soup, "og:image", "twitter:image")
    date = parse_date(_meta(soup, "article:published_time", "datePublished", "pubdate", "date")
                      or (ld.get("date") or ""))
    if not date:
        t = soup.find("time", attrs={"datetime": True})
        if t:
            date = parse_date(t["datetime"])

    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "form",
                     "iframe", "svg", "button", "select"]):
        tag.decompose()
    for el in soup.find_all(True, attrs={"class": True}):
        if getattr(el, "decomposed", False) or el.attrs is None:
            continue
        cls = " ".join(el.get("class") or []) + " " + (el.get("id") or "")
        if NOISE.search(cls) and el.name in ("div", "section", "ul", "aside"):
            if len(el.find_all("p")) < 8:  # katta matn blokini tasodifan o'chirmaslik
                el.decompose()

    body = ""
    cands = soup.find_all(["article"]) + soup.find_all(attrs={"itemprop": "articleBody"})
    cands += soup.find_all("div", class_=re.compile(r"(article|content|post|news|text|body|entry|story|single)", re.I))
    best, best_len = None, 0
    for c in cands:
        ps = c.find_all(["p", "li", "h2", "h3", "blockquote"])
        ln = sum(len(p.get_text(" ", strip=True)) for p in ps)
        if ln > best_len:
            best, best_len = c, ln
    # Sahifada bir nechta maqola bo'lsa (masalan, "o'xshash maqolalar" ochiq turgan bo'lsa),
    # sarlavha (h1) turgan blokni afzal ko'ramiz — matn shu maqolaga tegishli bo'lsin.
    h1 = soup.find("h1")
    if h1 is not None and best is not None and h1 not in best.descendants:
        node = h1.parent
        for _ in range(6):
            if node is None:
                break
            ln = sum(len(p.get_text(" ", strip=True)) for p in node.find_all("p"))
            if ln > 300:
                best, best_len = node, ln
                break
            node = node.parent
    if best is not None and best_len > 200:
        body = "\n".join(p.get_text(" ", strip=True) for p in best.find_all(["p", "li", "h2", "h3", "blockquote"]))
    else:
        body = "\n".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
    if ld.get("body") and len(ld["body"]) > len(body):
        body = ld["body"]
    return {"title": " ".join((title or "").split()), "desc": desc, "date": date, "text": body, "image": image}


# ---------------------------------------------------------------- sitemap
def parse_sitemap(xml: str):
    """(turi, [(loc, lastmod)]) — turi 'index' yoki 'urlset'."""
    soup = BeautifulSoup(xml, "xml")
    if soup.find("sitemapindex"):
        out = []
        for sm in soup.find_all("sitemap"):
            loc = sm.find("loc")
            lm = sm.find("lastmod")
            if loc:
                out.append((loc.get_text(strip=True), parse_date(lm.get_text(strip=True)) if lm else None))
        return "index", out
    out = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if not loc:
            continue
        lm = u.find("lastmod") or u.find("publication_date")
        title = u.find("title")
        out.append((loc.get_text(strip=True), parse_date(lm.get_text(strip=True)) if lm else None,
                    title.get_text(strip=True) if title else ""))
    return "urlset", out


# ---------------------------------------------------------------- Telegram
def tg_subscribers(html: str):
    """t.me/s/<kanal> sahifasidan obunachilar soni (topilmasa None)."""
    m = re.search(r'counter_value">([\d .,\u00a0]+?)\s*([KM]?)</span>\s*<span class="counter_type">(subscribers|members)', html)
    if not m:
        return None
    raw = re.sub(r"[\s\u00a0]", "", m.group(1))
    if m.group(2):
        v = float(raw.replace(",", "."))
    else:
        v = float(re.sub(r"[.,]", "", raw))
    return int(v * {"K": 1000, "M": 1000000}.get(m.group(2), 1))


def parse_tg(html: str):
    """t.me/s/<kanal> sahifasidagi postlar: [{post, text, date}]."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for m in soup.select("div.tgme_widget_message[data-post]"):
        tx = m.select_one(".tgme_widget_message_text")
        text = tx.get_text("\n", strip=True) if tx else ""
        t = m.select_one("time[datetime]")
        links = [a["href"] for a in (tx.select("a[href]") if tx else []) if a["href"].startswith("http")]
        prev = m.select_one("a.tgme_widget_message_link_preview[href]")
        if prev:
            links.append(prev["href"])
        out.append({"post": m["data-post"], "text": text, "links": links,
                    "date": parse_date(t["datetime"]) if t else None})
    return out
