"""Gazeta: kunlik va haftalik hisobot (faqat shaxsiy chatga, gazeta boti orqali).

Soatlik gazeta ishi oxirida chaqiriladi (gazeta.yml). 21:00 dan keyingi birinchi tekshiruvda — kunlik
hisobot; yakshanba kuni shu bilan birga — haftalik (dushanba–yakshanba). Har biri kuniga/haftasiga bir marta.
Manbalar: data/gazeta_runs.jsonl (har tekshiruv), data/gazeta_found.jsonl (topilmalar),
data/gazeta_orqaga_state.json (orqaga tekshirish holati). Yuborilgani: data/gazeta_hisobot.json.
"""
import os
import sys
import json
import html
import logging
import datetime as dt
from collections import Counter

from monitor.gazeta import ROOT, TZ, Bot

log = logging.getLogger("oav.gazeta.hisobot")
RUNS = ROOT / "data" / "gazeta_runs.jsonl"
FOUND = ROOT / "data" / "gazeta_found.jsonl"
ORQAGA = ROOT / "data" / "gazeta_orqaga_state.json"
SENT = ROOT / "data" / "gazeta_hisobot.json"
DAILY_HOUR = int(os.environ.get("GAZETA_HISOBOT_SOAT", "21"))
WEEKLY_DAY = 6          # yakshanba (dushanba = 0)


def _jsonl(p):
    out = []
    if p.exists():
        for line in p.read_text("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _t(s):
    try:
        t = dt.datetime.fromisoformat(s)
        return t.astimezone(TZ) if t.tzinfo else t.replace(tzinfo=TZ)
    except (TypeError, ValueError):
        return None


def _gap_text(sec):
    h, m = divmod(int(sec // 60), 60)
    return f"{h} soat {m:02d} daqiqa" if h else f"{m} daqiqa"


def period_stats(start, end):
    """[start, end) oralig'idagi tekshiruvlar va topilmalar."""
    runs = [r for r in _jsonl(RUNS) if (t := _t(r.get("t"))) and start <= t < end]
    allruns = sorted(t for r in _jsonl(RUNS) if (t := _t(r.get("t"))))
    found = []
    for r in _jsonl(FOUND):
        t = _t(r.get("found_at"))
        if t and start <= t < end:
            found.append(r)
    # eng uzun tanaffus: davr ichidagi tekshiruvlar orasida (davr boshidan oldingi oxirgisi ham hisobga olinadi)
    times = [t for t in allruns if start <= t < end]
    before = [t for t in allruns if t < start]
    seq = ([before[-1]] if before else []) + times
    gaps = [(b - a).total_seconds() for a, b in zip(seq, seq[1:])]
    return {
        "runs": len(runs),
        "files": sum(r.get("files", 0) for r in runs),
        "ocr": sum(r.get("ocr", 0) for r in runs),
        "err": sum(r.get("err", 0) for r in runs),
        "found": found,
        "max_gap": max(gaps) if gaps else None,
    }


def _orqaga_line():
    if not ORQAGA.exists():
        return None
    st = json.loads(ORQAGA.read_text("utf-8"))
    S = st.get("stats", {})
    if st.get("done"):
        return None
    return (f"Orqaga tekshirish ({st.get('from')} dan): {S.get('posts', 0)} post tekshirildi, "
            f"topilma {S.get('found', 0)} — davom etmoqda")


def daily_text(day):
    start = dt.datetime.combine(day, dt.time(0), TZ)
    s = period_stats(start, start + dt.timedelta(days=1))
    lines = [f"📊 <b>Gazeta — kunlik hisobot, {day.strftime('%d.%m.%Y')}</b>",
             f"Tekshirildi: {s['files']} ta fayl (gazeta/sahifa), shundan Gemini OCR: {s['ocr']}",
             f"Qo'mita tilga olingan: <b>{len(s['found'])}</b> ta gazetada"]
    for r in s["found"][:25]:
        pages = ", ".join(sorted({f"{i['page']}-bet" for i in r.get("items", []) if i.get("page")}))
        name = html.escape(f"{r.get('name') or 'Gazeta'}, {r.get('issue') or ''}".strip(", "), quote=False)
        lines.append(f"• {name}{' — ' + pages if pages else ''} — https://t.me/{r['key']}")
    if s["err"]:
        lines.append(f"⚠️ O'qilmay qolgan (Gemini xatosi): {s['err']}")
    gap = f", eng uzun tanaffus: {_gap_text(s['max_gap'])}" if s["max_gap"] else ""
    lines.append(f"Tekshiruvlar: {s['runs']} marta{gap}")
    ol = _orqaga_line()
    if ol:
        lines.append(ol)
    return "\n".join(lines)


def weekly_text(day):
    monday = day - dt.timedelta(days=day.weekday())
    start = dt.datetime.combine(monday, dt.time(0), TZ)
    end = start + dt.timedelta(days=7)
    s = period_stats(start, end)
    per = Counter((r.get("name") or "Gazeta") for r in s["found"])
    n_items = sum(len(r.get("items", [])) for r in s["found"])
    lines = [f"📈 <b>Gazeta — haftalik hisobot, {monday.strftime('%d.%m')}–{(end - dt.timedelta(days=1)).strftime('%d.%m.%Y')}</b>",
             f"Tekshirildi: {s['files']} ta fayl, shundan Gemini OCR: {s['ocr']}, o'qilmay qolgan: {s['err']}",
             f"Qo'mita tilga olingan: <b>{len(s['found'])}</b> ta gazetada, jami {n_items} ta joyda"]
    if per:
        lines.append("Gazetalar bo'yicha:")
        lines += [f"• {html.escape(n, quote=False)} — {c}" for n, c in per.most_common(20)]
    gap = f", eng uzun tanaffus: {_gap_text(s['max_gap'])}" if s["max_gap"] else ""
    lines.append(f"Tekshiruvlar: {s['runs']} marta{gap}")
    return "\n".join(lines)


def main(now=None, dm=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = now or dt.datetime.now(TZ)
    dm = dm or Bot(os.environ.get("GAZETA_BOT_TOKEN"), os.environ.get("TG_CHAT_ID"))
    sent = json.loads(SENT.read_text("utf-8")) if SENT.exists() else {}
    today = now.date()
    if now.hour < DAILY_HOUR:
        log.info("Hisobot vaqti emas (%02d:00 dan keyin)", DAILY_HOUR)
        return
    changed = False
    if sent.get("daily") != today.isoformat():
        if dm.send(daily_text(today)):
            sent["daily"] = today.isoformat()
            changed = True
            log.info("Kunlik hisobot yuborildi")
    week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
    if today.weekday() == WEEKLY_DAY and sent.get("weekly") != week:
        if dm.send(weekly_text(today)):
            sent["weekly"] = week
            changed = True
            log.info("Haftalik hisobot yuborildi")
    if changed:
        SENT.write_text(json.dumps(sent), "utf-8")


if __name__ == "__main__":
    main()
    sys.exit(0)
