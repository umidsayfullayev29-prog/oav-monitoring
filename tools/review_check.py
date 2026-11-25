"""backfill_review.md dagi sanasiz topilmalarni tekshirish: sana (meta, JSON-LD, <time>, matndagi sana) va kalit so'z joyi."""
import re, sys, json, datetime as dt
from pathlib import Path
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor import web
from monitor.textutil import Matcher, snippet

ROOT = Path(__file__).resolve().parent.parent
kw = Matcher(yaml.safe_load((ROOT / "config/keywords.yml").read_text("utf-8")))
urls = re.findall(r"\]\((https?://[^)]+)\)", (ROOT / "data/backfill_review.md").read_text("utf-8"))
MONTHS = {m: i+1 for i, m in enumerate("января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split())}
rows = []
for u in urls:
    try:
        final, text = web.fetch(u, 20)
    except Exception as e:  # noqa
        rows.append((u, "OCHILMADI", str(e)[:60], "")); continue
    dates = []
    for pat in [r'"datePublished"\s*:\s*"([^"]+)"', r'property="article:published_time"\s+content="([^"]+)"',
                r'content="([^"]+)"\s+property="article:published_time"', r'<time[^>]*datetime="([^"]+)"',
                r'itemprop="datePublished"[^>]*content="([^"]+)"']:
        dates += re.findall(pat, text)
    vis = re.findall(r"\b(\d{1,2})[./](\d{1,2})[./](20\d\d)\b", text)
    vis2 = re.findall(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(20\d\d)", text)
    art = web.extract_article(text)
    full = "\n".join(x for x in (art["title"], art.get("desc") or "", art["text"]) if x)
    m = kw.match(full, title=art["title"] or "", trusted=True)
    sn = snippet(full, m[2], m[3]).replace("<b>", "**").replace("</b>", "**")[:220] if m else "KALIT SO'Z YO'Q"
    d = (dates[0][:10] if dates else "") or (art["date"].date().isoformat() if art.get("date") else "")
    rows.append((u, d or "?", f"vis={vis[:2]} {vis2[:2]}", sn))
out = ["| URL | Sana | Matndagi sanalar | Parcha |", "|---|---|---|---|"]
for r in rows:
    out.append("| " + " | ".join(str(x).replace("|", "/").replace("\n", " ") for x in r) + " |")
txt = "\n".join(out)
(ROOT / "data/review_check.md").write_text(txt + "\n", "utf-8")
print(txt)
