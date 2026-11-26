"""data/missed_send.jsonl — eski "bir xil xabar" xatosi tufayli kanalga bormay qolgan topilmalarni yuborish.

Rejimlar: dry — hech narsa yubormaydi, data/missed_report.md ga nima ketishini yozadi;
          send — kanalga yuboradi. Yuborilganlar data/backfill_sent.txt ga yoziladi (qayta yuborilmaydi).
Telegram kanallari: 5000 dan kam yoki obunachisi aniqlanmagan kanal yuborilmaydi.
"""
import os
import sys
import json
import time
import html
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import yaml  # noqa: E402
from monitor.main import (DATA, CHANNEL_START, ARCHIVE_CHAT, EXCLUDE_TG, problem_reason,  # noqa: E402
                          channel_text)
from monitor import web  # noqa: E402
from monitor.telegram import Telegram  # noqa: E402

log = logging.getLogger("oav.missed")


def load(p):
    out = []
    if p.exists():
        for line in p.read_text("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def main(mode):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    items = load(DATA / "missed_send.jsonl")
    sent_p = DATA / "backfill_sent.txt"
    sent = set(sent_p.read_text("utf-8").split()) if sent_p.exists() else set()
    live = set()
    for r in load(DATA / "found.jsonl"):
        if (r.get("found_at") or "") >= CHANNEL_START and not r.get("alt_of") and r.get("kind") != "taxminiy":
            live.add(r["k"])
    min_subs = yaml.safe_load((ROOT / "config/sources.yml").read_text("utf-8")).get(
        "telegram_channels_min_subscribers", 5000)
    subs = {}
    todo, skipped = [], []
    for r in items:
        if r["k"] in sent or r["k"] in live:
            skipped.append((r, "oldin yuborilgan"))
            continue
        if problem_reason(r):
            skipped.append((r, problem_reason(r)))
            continue
        if r.get("layer") == "Telegram":
            c = r["url"].split("/")[3]
            if c.lower() in EXCLUDE_TG:
                skipped.append((r, "qo'mita kanali"))
                continue
            if c not in subs:
                try:
                    subs[c] = web.tg_subscribers(web.fetch(f"https://t.me/s/{c}", 15)[1])
                except Exception as e:  # noqa
                    log.warning("%s: obunachi aniqlanmadi: %s", c, str(e)[:80])
                    subs[c] = None
            if subs[c] is None or subs[c] < min_subs:
                skipped.append((r, f"obunachi: {subs[c]}"))
                continue
        todo.append(r)
    todo.sort(key=lambda r: r.get("date") or r.get("found_at") or "")
    log.info("Jami: %d, yuboriladi: %d, o'tkaziladi: %d", len(items), len(todo), len(skipped))

    lines = [f"# Kanalga bormay qolgan topilmalar ({mode})", "",
             f"Yuboriladi: {len(todo)}, o'tkaziladi: {len(skipped)}", "", "## Yuboriladi", ""]
    for r in todo:
        lines.append(f"- {(r.get('date') or '')[:10]} · {r['url']} · {(r.get('title') or '')[:90]}")
    lines += ["", "## O'tkaziladi", ""]
    for r, why in skipped:
        lines.append(f"- {r['url']} — {why}")
    (DATA / "missed_report.md").write_text("\n".join(lines) + "\n", "utf-8")
    if mode != "send":
        return

    token = os.environ.get("TG_BOT_TOKEN")
    ch = Telegram(token, ARCHIVE_CHAT)
    n_ok = n_fail = 0
    deadline = time.monotonic() + int(os.environ.get("SEND_BUDGET_SEC", "2100"))
    with sent_p.open("a", encoding="utf-8") as f:
        for r in todo:
            if time.monotonic() > deadline:
                log.warning("Vaqt tugadi — qolganlari keyingi ishga tushishda yuboriladi")
                break
            url = r["url"]
            if ch.send(channel_text(r, url), silent=True, preview_url=url):
                f.write(r["k"] + "\n")
                f.flush()
                n_ok += 1
            else:
                n_fail += 1
                log.warning("Yuborilmadi: %s", url)
            time.sleep(3.2)   # kanalga daqiqasiga 20 tadan oshmasin
    left = len(todo) - n_ok - n_fail
    log.info("Kanalga yuborildi: %d, xato: %d, qoldi: %d", n_ok, n_fail, left)
    dm = Telegram(token, os.environ.get("TG_CHAT_ID"))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    dm.send("\n".join([
        "🔁 <b>Bormay qolgan topilmalar (bir xil xabar xatosi)</b>",
        f"Kanalga yuborildi: <b>{n_ok}</b>" + (f", xato: {n_fail}" if n_fail else "") + (f", qoldi: {left}" if left else ""),
        f"O'tkazildi: {len(skipped)}",
        html.escape(f"https://github.com/{repo}/blob/main/data/missed_report.md") if repo else "",
    ]), silent=False, preview=False)
    if n_fail or left:
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dry")
