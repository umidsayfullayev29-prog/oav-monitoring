import sys, re, concurrent.futures as cf
sys.path.insert(0, '.')
from monitor import web
def chk(n):
    try:
        _, h = web.fetch(f"https://t.me/s/{n}", 15)
    except Exception as e:
        return f"{n} ERR"
    posts = len(re.findall(r'tgme_widget_message_wrap', h))
    t = re.search(r'<meta property="og:title" content="([^"]*)"', h)
    return f"{n} posts={posts} subs={web.tg_subscribers(h)} | {t.group(1) if t else ''}"
with cf.ThreadPoolExecutor(8) as p:
    for r in p.map(chk, sys.argv[1:]): print("TG", r, flush=True)
