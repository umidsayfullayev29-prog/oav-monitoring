"""data/manual_send.json dagi qo'lda tasdiqlangan topilmalarni arxiv kanaliga yuborish (bir marta)."""
import os, sys, json, time, html
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor.telegram import Telegram
ROOT = Path(__file__).resolve().parent.parent
items = json.loads((ROOT / "data/manual_send.json").read_text("utf-8"))
sent_p = ROOT / "data/manual_sent.txt"
sent = set(sent_p.read_text().split()) if sent_p.exists() else set()
ch = Telegram(os.environ.get("TG_BOT_TOKEN"), os.environ.get("ARCHIVE_CHAT"))
with sent_p.open("a") as f:
    for it in items:
        if it["url"] in sent:
            continue
        lines = [html.escape(it["url"]), f"📰 <b>{html.escape(it['title'])}</b>"]
        if it.get("date"):
            y, m, d = it["date"].split("-")
            lines.append(f"🗓 {d}.{m}.{y}")
        if ch.send("\n".join(lines), silent=True, preview_url=it["url"]):
            f.write(it["url"] + "\n"); f.flush()
            print("OK", it["url"])
        time.sleep(3.2)
