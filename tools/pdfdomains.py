"""Vaqtinchalik: 'OAV biz haqimizda' PDF jadvallaridan OAV domenlarini yig'ish."""
import sys, re, io, collections
sys.path.insert(0, '.')
import requests, pdfplumber
from monitor import web
urls = sys.argv[1:]
dom = collections.Counter()
names = collections.Counter()
for u in urls:
    try:
        r = web.session().get(u, timeout=60, verify=False)
        pdf = pdfplumber.open(io.BytesIO(r.content))
    except Exception as e:
        print("ERR", u, e); continue
    n = 0
    for page in pdf.pages:
        for link in (page.hyperlinks or []):
            h = link.get("uri") or ""
            d = web.host_of(h)
            if d: dom[d] += 1; n += 1
        txt = page.extract_text() or ""
        for m in re.finditer(r"https?://[^\s)\]]+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:uz|ru|com|org|net|info|news|media|tv|kz|asia|agency|report|am|az|tj|kg)\b(?:/[^\s]*)?", txt, re.I):
            d = web.host_of(m.group(0) if m.group(0).startswith("http") else "https://" + m.group(0))
            if d: dom[d] += 1; n += 1
        for t in page.extract_tables() or []:
            for row in t:
                for cell in row or []:
                    if cell and len(cell) < 40 and not re.search(r"\d{2}\.\d{2}|http", cell):
                        names[" ".join(cell.split())] += 1
    print("PDF", u.split('/')[-1], "pages", len(pdf.pages), "links", n)
print("DOMAINS", len(dom))
for d, c in dom.most_common(): print("D", c, d)
print("NAMES")
for nm, c in names.most_common(150): print("N", c, nm)
