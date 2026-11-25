"""Maqola sahifasidan ajratilgan matnni ko'rish: python tools/peek_art.py URL -> data/peek.md"""
import sys
from monitor import web

url = sys.argv[1]
final, html = web.fetch(url, 20)
a = web.extract_article(html)
out = [f"# {a['title']}", f"URL: {final}", f"Sana: {a['date']}", f"Rasm: {a['image']}", "", "## Matn", a["text"],
       "", "## Lenta qatorlari (olib tashlangan)"]

from bs4 import BeautifulSoup
raw = BeautifulSoup(html, "lxml").get_text("\n", strip=True)
out += [ln for ln in raw.split("\n") if web.FEED_TS.search(ln)][:40]
open("data/peek.md", "w").write("\n".join(out))
print("\n".join(out)[:4000])
