"""Kanal postlari tuzilishini ko'rish: matn, rasm soni, hujjat (PDF) nomlari, sana."""
import sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bs4 import BeautifulSoup
from monitor import web
chan = sys.argv[1]
_, html = web.fetch(f"https://t.me/s/{chan}", 20)
soup = BeautifulSoup(html, "lxml")
out = [f"# {chan}", ""]
for m in soup.select("div.tgme_widget_message[data-post]")[-20:]:
    t = m.select_one("time[datetime]")
    tx = m.select_one(".tgme_widget_message_text")
    photos = m.select(".tgme_widget_message_photo_wrap")
    docs = [d.get_text(" ", strip=True) for d in m.select(".tgme_widget_message_document_title")]
    extra = [d.get_text(" ", strip=True) for d in m.select(".tgme_widget_message_document_extra")]
    vids = m.select(".tgme_widget_message_video_player")
    grouped = m.select(".tgme_widget_message_grouped_wrap")
    out.append(f"## {m['data-post']} · {t['datetime'] if t else ''}")
    out.append(f"rasmlar: {len(photos)}, video: {len(vids)}, albom: {bool(grouped)}, hujjatlar: {docs} {extra}")
    out.append("matn: " + (tx.get_text(' ', strip=True)[:400] if tx else "(yo'q)"))
    if photos:
        st = photos[0].get("style", "")
        mm = re.search(r"url\('([^']+)", st)
        out.append("1-rasm: " + (mm.group(1) if mm else st[:100]))
    out.append("")
Path("data/peek.md").write_text("\n".join(out), "utf-8")
print("\n".join(out))
