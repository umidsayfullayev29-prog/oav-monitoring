"""Vaqtinchalik: nomzod saytlarni tekshirish — ochiladimi, maqola havolalari bormi."""
import sys, concurrent.futures as cf
sys.path.insert(0, '.')
from monitor import web
def check(d):
    try:
        final, html = web.fetch(f"https://{d}/", 20)
    except Exception as e:
        return f"FAIL {d} {type(e).__name__}"
    host = web.host_of(final)
    links, feeds = web.extract_links(html, final, host)
    return f"OK {d} {host} links={len(links)} feeds={len(feeds)}"
with cf.ThreadPoolExecutor(24) as p:
    for r in p.map(check, sys.argv[1:]):
        print(r, flush=True)
