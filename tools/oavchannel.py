"""Vaqtinchalik: t.me/s/statistikaoav tarixidan manba kanallar va saytlarni yig'ish."""
import sys, re, time, collections
sys.path.insert(0, '.')
from bs4 import BeautifulSoup
from monitor import web
chan = sys.argv[1]; pages = int(sys.argv[2])
fwd = collections.Counter(); tlinks = collections.Counter(); doms = collections.Counter()
before = None; oldest = None
for i in range(pages):
    url = f"https://t.me/s/{chan}" + (f"?before={before}" if before else "")
    try:
        _, html = web.fetch(url, 20)
    except Exception as e:
        print("ERR", url, e); time.sleep(5); continue
    soup = BeautifulSoup(html, "lxml")
    msgs = soup.select("div.tgme_widget_message[data-post]")
    if not msgs: break
    ids = [int(m["data-post"].split("/")[-1]) for m in msgs]
    for m in msgs:
        f = m.select_one("a.tgme_widget_message_forwarded_from_name")
        if f and f.get("href"):
            mm = re.search(r"t\.me/([A-Za-z][A-Za-z0-9_]{3,})", f["href"])
            if mm: fwd[mm.group(1).lower()] += 1
        for a in m.select(".tgme_widget_message_text a[href]"):
            h = a["href"]
            mm = re.search(r"t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,})", h)
            if mm: tlinks[mm.group(1).lower()] += 1
            elif h.startswith("http"):
                d = web.host_of(h)
                if d: doms[d] += 1
        t = m.select_one("time[datetime]")
        if t: oldest = t["datetime"]
    before = min(ids)
    if before <= 1: break
    time.sleep(0.4)
print("PAGES", i + 1, "OLDEST", oldest)
for n, c in fwd.most_common(): print("FWD", c, n)
for n, c in tlinks.most_common(): print("TL", c, n)
for n, c in doms.most_common(): print("DOM", c, n)
