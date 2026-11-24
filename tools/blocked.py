"""Vaqtinchalik: chet eldan ochilmaydigan saytlarni turli yo'llar bilan ochib ko'rish."""
import sys, re, concurrent.futures as cf
sys.path.insert(0, '.')
from monitor import web
def tg(d):  # translate.goog proxy
    h = d.replace('-', '--').replace('.', '-')
    return f"https://{h}.translate.goog/?_x_tr_sl=uz&_x_tr_tl=uz&_x_tr_hl=uz&_x_tr_pto=wapp"
def check(d):
    out = []
    for name, url in (("direct", f"https://{d}/"), ("http", f"http://{d}/"), ("gtrans", tg(d)), ("jina", f"https://r.jina.ai/https://{d}/")):
        try:
            final, html = web._fetch(url, (8, 20))
            links = len(re.findall(r'href=', html)) if name != "jina" else len(re.findall(r'\]\(https?://', html))
            out.append(f"{name}=OK({len(html)//1000}k,{links})")
        except Exception as e:
            out.append(f"{name}=ERR({type(e).__name__}:{str(e)[:40]})")
    return d + " | " + " ".join(out)
with cf.ThreadPoolExecutor(10) as p:
    for r in p.map(check, sys.argv[1:]):
        print("RES", r, flush=True)
