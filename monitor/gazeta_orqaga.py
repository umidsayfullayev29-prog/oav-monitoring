"""Gazeta: orqaga tekshirish (01.11 dan) — bo'lib-bo'lib, soatlik gazeta monitoringidan alohida.

Soatlik ish (monitor/gazeta.py, data/gazeta_state.json) bilan umumiy holat yo'q: o'z holati
data/gazeta_orqaga_state.json, o'z topilmalar fayli data/gazeta_orqaga_found.jsonl. Usul o'sha:
PDF matni + kalit so'z, eski shrift tuzatiladi, o'qilmasa/rasm bo'lsa — Gemini faqat matnni o'qiydi.
Topilmalar faqat shaxsiy chatga, "🗞 Gazeta (orqaga)" belgisi bilan.

Soatlik gazeta ishi ichida (gazeta.yml), qolgan vaqt (ORQAGA_BUDGET_SEC) tugaguncha ishlaydi va qayerda to'xtaganini saqlaydi;
keyingi soatda shu joydan davom etadi. Tugagach — shaxsiy chatga yakuniy xabar.
Muhit: TG_API_ID, TG_API_HASH, TG_SESSION, GAZETA_GEMINI_KEY, GAZETA_BOT_TOKEN, TG_CHAT_ID,
ORQAGA_FROM (YYYY-MM-DD), ORQAGA_TO_ID (shu post raqamigacha, shu jumladan).
"""
import os
import sys
import json
import time
import html
import asyncio
import logging
import datetime as dt

import requests

from monitor.gazeta import (ROOT, TZ, CHANNEL, Bot, media_kind, analyse_unit, post_text,
                            paper_name, paper_date)

log = logging.getLogger("oav.gazeta.orqaga")
STATE = ROOT / "data" / "gazeta_orqaga_state.json"
FOUND = ROOT / "data" / "gazeta_orqaga_found.jsonl"
BUDGET = int(os.environ.get("ORQAGA_BUDGET_SEC", "2400"))
MAX_TRIES = 3


def load():
    if STATE.exists():
        return json.loads(STATE.read_text("utf-8"))
    return {}


def save(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")


async def collect(client, entity, st):
    """Hali tekshirilmagan postlar (albomlar bo'yicha), eskisidan yangisiga."""
    to_id = st["to_id"]
    msgs = []
    if st.get("cursor"):
        it = client.iter_messages(entity, min_id=st["cursor"], max_id=to_id + 1, reverse=True, limit=None)
    else:
        start = dt.datetime.fromisoformat(st["from"]).replace(tzinfo=TZ)
        it = client.iter_messages(entity, offset_date=start, max_id=to_id + 1, reverse=True, limit=None)
    async for m in it:
        if m.id > to_id:
            break
        msgs.append(m)
    units, by_group = [], {}
    for m in msgs:
        key = m.grouped_id or f"m{m.id}"
        if key not in by_group:
            by_group[key] = {"ids": [], "caption": ""}
            units.append(by_group[key])
        u = by_group[key]
        u["ids"].append(m.id)
        if m.message:
            u["caption"] = (u["caption"] + "\n" + m.message).strip()
    return units


async def main_async():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id, api_hash, sess = (os.environ.get(k, "").strip() for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"))
    if not (api_id and api_hash and sess and os.environ.get("GAZETA_BOT_TOKEN", "").strip()):
        log.warning("Gazeta secretlari yo'q — o'tkazib yuborildi")
        return "TUGADI"
    st = load()
    if not st:
        st = {"from": os.environ.get("ORQAGA_FROM", "2026-11-01"), "to_id": int(os.environ.get("ORQAGA_TO_ID", "0")),
              "cursor": 0, "retry": [], "sent": [], "stats": {"posts": 0, "files": 0, "found": 0, "err": 0},
              "started": dt.datetime.now(TZ).isoformat(timespec="minutes")}
    if st.get("done"):
        log.info("Orqaga tekshirish allaqachon tugagan")
        return "TUGADI"
    if not st["to_id"]:
        log.error("ORQAGA_TO_ID berilmagan")
        return "TUGADI"
    t0 = time.monotonic()
    dm = Bot(os.environ.get("GAZETA_BOT_TOKEN"), os.environ.get("TG_CHAT_ID"))
    sent = set(st["sent"])
    session = requests.Session()
    S = st["stats"]
    broke, rest = False, []

    def strip(x):
        return {k: v for k, v in x.items() if k != "new"}

    async with TelegramClient(StringSession(sess), int(api_id), api_hash) as client:
        entity = await client.get_entity(CHANNEL)
        units = await collect(client, entity, st)
        retry = [r for r in st.get("retry", []) if r.get("tries", 0) < MAX_TRIES]
        queue = [{"ids": u["ids"], "caption": u["caption"], "tries": 0, "new": True} for u in units] + \
                [{**r, "new": False} for r in retry]
        log.info("Navbat: %d yangi post, %d qayta urinish (cursor=%s)", len(units), len(retry), st.get("cursor"))
        new_retry = []
        for qi, u in enumerate(queue):
            if time.monotonic() - t0 > BUDGET:
                log.info("Vaqt tugadi — keyingi qism davom ettiradi")
                broke, rest = True, [strip(x) for x in queue[qi:] if not x["new"]]
                break
            ms = [m for m in await client.get_messages(entity, ids=u["ids"]) if m]
            items = []
            for m in ms:
                kind = media_kind(m)
                if not kind:
                    continue
                data = await client.download_media(m, file=bytes)
                fname = ""
                for a in (getattr(getattr(m, "document", None), "attributes", None) or []):
                    fname = getattr(a, "file_name", "") or fname
                if data:
                    items.append({"id": m.id, "kind": kind, "data": data, "fname": fname})
            if items:
                S["posts"] += 1
                S["files"] += len(items)
                res = analyse_unit({"msgs": items, "caption": u["caption"]}, session)
                if res is None:
                    S["err"] += 1
                    tries = u.get("tries", 0) + 1
                    if tries < MAX_TRIES:
                        new_retry.append({"ids": u["ids"], "caption": u["caption"], "tries": tries})
                    else:
                        link = f"https://t.me/{CHANNEL}/{min(u['ids'])}"
                        dm.send("⚠️ Gazeta (orqaga): post o'qilmadi (Gemini xatosi), o'tkazib yuborildi:\n" + html.escape(link))
                else:
                    found, meta = res
                    by_msg = {}
                    for it in found:
                        by_msg.setdefault(it["msg_id"], []).append(it)
                    for mid, its in by_msg.items():
                        key = f"{CHANNEL}/{mid}"
                        if key in sent:
                            continue
                        msg = next((m for m in ms if m.id == mid), ms[0])
                        fname = next((x["fname"] for x in items if x["id"] == mid), "")
                        name = (meta.get("newspaper") or "").strip() or paper_name(u["caption"], fname)
                        issue = (paper_date(u["caption"], fname, meta.get("date") or "")
                                 or msg.date.astimezone(TZ).strftime("%d.%m.%Y"))
                        if dm.send("🗞 Gazeta (orqaga)\n" + post_text(f"https://t.me/{CHANNEL}/{mid}", name, issue, its)):
                            sent.add(key)
                            S["found"] += 1
                            with FOUND.open("a", encoding="utf-8") as f:
                                f.write(json.dumps({"key": key, "date": msg.date.isoformat(), "name": name,
                                                    "issue": issue, "items": its}, ensure_ascii=False) + "\n")
                        else:
                            new_retry.append({"ids": u["ids"], "caption": u["caption"], "tries": u.get("tries", 0) + 1})
            if u["new"]:
                st["cursor"] = max(st.get("cursor") or 0, max(u["ids"]))
            st["sent"] = sorted(sent)
            st["retry"] = new_retry + [strip(x) for x in queue[qi + 1:] if not x["new"]]
            save(st)                     # har postdan keyin — vaqt limitida ham natija yo'qolmaydi
        st["retry"] = new_retry + rest
        st["done"] = not broke and not st["retry"]
        save(st)

    log.info("Orqaga: %s, cursor=%s, retry=%d, done=%s", S, st.get("cursor"), len(st.get("retry", [])), st.get("done"))
    if st.get("done"):
        dm.send(f"✅ Gazeta (orqaga) tugadi: {st['from']} dan {st['to_id']}-postgacha. "
                f"{S['posts']} post, {S['files']} fayl, topilma {S['found']}, o'qilmagan {S['err']}.")
        return "TUGADI"
    return "DAVOM"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    r = asyncio.run(main_async())
    print("NATIJA", r)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"natija={r}\n")


if __name__ == "__main__":
    main()
