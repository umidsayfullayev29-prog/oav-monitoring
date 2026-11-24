"""Vaqtinchalik: sahifa tuzilmasini ko'rish uchun (stat.uz 'OAV biz haqimizda')."""
import sys, re
sys.path.insert(0, '.')
from monitor import web
from bs4 import BeautifulSoup
for url in sys.argv[1:]:
    print("=" * 30, url)
    try:
        final, html = web.fetch(url, 30)
    except Exception as e:
        print("ERR", e); continue
    print("LEN", len(html), final)
    soup = BeautifulSoup(html, "lxml")
    for f in soup.find_all(["iframe", "embed", "object"]):
        print("FRAME", f.get("src") or f.get("data"))
    for i, m in enumerate(re.finditer(r"Loading", html)):
        if i > 3: break
        print("CTX", html[max(0, m.start()-1500):m.start()+300].replace("\n", " ")[:1800])
    for s in soup.find_all("script"):
        t = (s.get("src") or "") + " " + (s.string or "")[:300]
        if re.search(r"sheet|docs\.google|ajax|json|table|xls|load", t, re.I):
            print("SCRIPT", t.replace("\n", " ")[:400])
    body = soup.find(class_=re.compile("item-page|article|content", re.I))
    if body:
        for a in body.find_all("a", href=True)[:200]:
            print("A", a["href"], "|", a.get_text(" ", strip=True)[:60])
