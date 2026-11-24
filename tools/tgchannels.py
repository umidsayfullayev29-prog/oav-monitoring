"""Vaqtinchalik: 'OAV biz haqimizda' PDF'laridan Telegram kanallarini yig'ib, ochiqligini tekshirish."""
import sys, re, io, collections, concurrent.futures as cf
sys.path.insert(0, '.')
import pdfplumber
from monitor import web
cnt = collections.Counter()
for u in sys.argv[1:]:
    try:
        r = web.session().get(u, timeout=60, verify=False)
        pdf = pdfplumber.open(io.BytesIO(r.content))
    except Exception as e:
        print("ERR", u, e); continue
    for page in pdf.pages:
        uris = [l.get("uri") or "" for l in (page.hyperlinks or [])]
        uris += re.findall(r"t\.me/[A-Za-z0-9_+/]+", page.extract_text() or "")
        for h in uris:
            m = re.search(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,})", h)
            if m and m.group(1).lower() not in ("joinchat", "share", "addstickers", "c"):
                cnt[m.group(1).lower()] += 1
print("TOTAL", len(cnt))
def check(name):
    try:
        _, html = web.fetch(f"https://t.me/s/{name}", 20)
    except Exception as e:
        return name, -1, type(e).__name__
    n = len(re.findall(r'class="tgme_widget_message_wrap', html))
    title = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    return name, n, (title.group(1) if title else "")
names = [n for n, c in cnt.most_common() if c >= 3]
with cf.ThreadPoolExecutor(6) as p:
    for name, n, t in p.map(check, names):
        print(f"CH {cnt[name]} {name} posts={n} | {t}", flush=True)
