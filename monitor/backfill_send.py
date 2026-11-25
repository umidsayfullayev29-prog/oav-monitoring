"""Qayta ko'rib chiqish natijalarini (data/backfill.jsonl) arxiv kanaliga yuborish.

- Kanalga allaqachon ketganlar (data/found.jsonl, kanal davri) o'tkaziladi.
- Bitta nashrning bir nechta til versiyasidan bittasi — o'zbekchasi — yuboriladi.
- Google havolasi ochilmagan, taxminiy va sanasi noma'lum topilmalar kanalga emas,
  data/backfill_review.md fayliga yoziladi.
- Yuborilganlar data/backfill_sent.txt ga yozib boriladi: to'xtab qolsa, qayta ishga tushganda davom etadi.
"""
import os
import sys
import json
import time
import html
import logging
from pathlib import Path

from .main import (DATA, CHANNEL_START, LANG_PRIORITY, ARCHIVE_CHAT, problem_reason, channel_text,
                   h, NOW)
from . import web
from . import versions as ver
from .telegram import Telegram

log = logging.getLogger("oav.bf")


def load(p):
    out = []
    if p.exists():
        for line in p.read_text("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    raw = []
    for fp in sorted((DATA / "bf").glob("*.jsonl")):
        raw += load(fp)
    # qismlar orasidagi takrorlarni olib tashlash va til/kanal versiyalarini qayta guruhlash
    seen, recs = set(), []
    # to'liq matn bilan topilganlar (sayt, Telegram) Google/Bing nusxasidan ustun
    for r in sorted(raw, key=lambda r: (r.get("layer") in ("Google News", "Bing News"),
                                        r.get("date") or r.get("found_at") or "")):
        keys = {r["k"]} | set(r.get("xk") or []) | ({r["tk"]} if r.get("tk") else set())
        if keys & seen:
            continue
        seen |= keys
        r.pop("alt_of", None)
        for o in recs:
            if "news.google." not in r["url"] and o.get("outlet") == r.get("outlet") and ver.same_story(r, o):
                r["alt_of"] = o.get("alt_of") or o["k"]
                break
        recs.append(r)
    sent_p = DATA / "backfill_sent.txt"
    sent = set(sent_p.read_text("utf-8").split()) if sent_p.exists() else set()
    # kanalga jonli monitoring yuborganlar (qayta ko'rish davomida yuborilganlari ham)
    live = set()
    for r in load(DATA / "found.jsonl"):
        if (r.get("found_at") or "") >= CHANNEL_START and r.get("kind") != "taxminiy" \
                and "news.google." not in (r.get("url") or ""):
            live.add(r.get("k"))
            live.update(r.get("xk") or [])
            if r.get("tk"):
                live.add(r["tk"])
    groups = {}
    for r in recs:
        groups.setdefault(r.get("alt_of") or r["k"], []).append(r)
    chosen, review = [], []
    for root, grp in groups.items():
        if any(x["k"] in live or x.get("tk") in live for x in grp):
            continue
        ok = [x for x in grp if not problem_reason(x) and (x.get("date") or x.get("layer") == "Telegram")]
        if not ok:
            review.append(grp[0])
            continue
        best = min(ok, key=lambda x: (LANG_PRIORITY.get(x.get("lang"), 9), grp.index(x)))
        chosen.append(best)
    chosen.sort(key=lambda r: r.get("date") or r.get("found_at") or "")
    log.info("Jami topilma: %d, guruh: %d, kanalga: %d, ko'rib chiqish uchun: %d, oldin yuborilgan: %d",
             len(recs), len(groups), len(chosen), len(review), len(sent))

    # ko'rib chiqish ro'yxati
    lines = ["# Qayta ko'rib chiqish: tekshirish kerak bo'lgan topilmalar", "",
             f"Yaratildi: {NOW.strftime('%d.%m.%Y %H:%M')}. Bular kanalga yuborilmadi.", ""]
    for r in sorted(review, key=lambda r: r.get("date") or ""):
        why = problem_reason(r) or "Sanasi aniqlanmadi"
        lines.append(f"- {(r.get('date') or '')[:10]} · {r.get('source')} · [{r.get('title','')[:120]}]({r['url']}) — {why}")
    (DATA / "backfill_review.md").write_text("\n".join(lines) + "\n", "utf-8")

    token = os.environ.get("TG_BOT_TOKEN")
    ch = Telegram(token, ARCHIVE_CHAT)
    n_ok = n_fail = 0
    deadline = time.monotonic() + int(os.environ.get("SEND_BUDGET_SEC", "15000"))
    with sent_p.open("a", encoding="utf-8") as f:
        for r in chosen:
            if r["k"] in sent:
                continue
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
            time.sleep(3.2)   # kanalga daqiqasiga 20 tadan oshmasin
    left = sum(1 for r in chosen if r["k"] not in sent) - n_ok
    log.info("Kanalga yuborildi: %d, xato: %d, qoldi: %d", n_ok, n_fail, left)
    dm = Telegram(token, os.environ.get("TG_CHAT_ID"))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    dm.send("\n".join([
        "📚 <b>1-noyabrdan qayta ko'rib chiqish</b>",
        f"Kanalga yuborildi: <b>{n_ok}</b>" + (f" (qoldi: {left})" if left > 0 else ""),
        f"Tekshirish kerak (kanalga ketmadi): <b>{len(review)}</b>",
        f"https://github.com/{repo}/blob/main/data/backfill_review.md" if repo else "",
    ]), silent=False, preview=False)


if __name__ == "__main__":
    main()
