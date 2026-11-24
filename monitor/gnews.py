"""Google News vositachi havolasidan (news.google.com/rss/articles/...) asl maqola havolasini olish.

1) Eski formatdagi id ichida havola base64 ko'rinishida turadi — to'g'ridan-to'g'ri ochiladi.
2) Yangi format (AU_yqL...) uchun Google'ning o'z sahifasidan imzo (sg) va vaqt (ts) olinib,
   batchexecute so'rovi yuboriladi — javobda asl havola keladi.
Har qanday xatoda None qaytadi (topilma kanalga emas, egasiga "tekshiring" bo'lib boradi).
"""
import re
import json
import base64
import logging
import urllib.parse as up

import requests

log = logging.getLogger("oav.gnews")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _gid(url):
    p = up.urlsplit(url)
    if "news.google." not in (p.hostname or ""):
        return None
    parts = [x for x in p.path.split("/") if x]
    for i, x in enumerate(parts):
        if x in ("articles", "read") and i + 1 < len(parts):
            return parts[i + 1]
    return None


def _old_format(gid):
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
    except Exception:  # noqa
        return None
    m = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not m:
        return None
    u = m.group(0).decode("ascii", "ignore")
    # protobuf qoldiqlari: havoladan keyin keladigan boshqaruv baytlari kesiladi
    u = re.split(r"[\x00-\x20\xd2]", u)[0]
    if u.startswith("AU_yqL") or len(u) < 12:
        return None
    return u


def decode(url, session=None, timeout=15):
    gid = _gid(url)
    if not gid:
        return None
    if not gid.startswith("AU_yqL"):
        u = _old_format(gid)
        if u:
            return u
    s = session or requests.Session()
    hdr = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    sg = ts = None
    for page in (f"https://news.google.com/articles/{gid}", f"https://news.google.com/rss/articles/{gid}"):
        try:
            r = s.get(page, headers=hdr, timeout=timeout)
        except requests.RequestException as e:
            log.info("gnews sahifa xato: %s", e)
            continue
        if r.status_code != 200:
            log.info("gnews sahifa %s: %s", r.status_code, page[:80])
            continue
        m1 = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        m2 = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if m1 and m2:
            sg, ts = m1.group(1), m2.group(1)
            break
    if not sg:
        return None
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{gid}",{ts},"{sg}"]')
    freq = json.dumps([[["Fbv4je", inner, None, "generic"]]])
    try:
        r = s.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                   headers={**hdr, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                   data="f.req=" + up.quote(freq), timeout=timeout)
    except requests.RequestException as e:
        log.info("gnews batchexecute xato: %s", e)
        return None
    if r.status_code != 200:
        log.info("gnews batchexecute %s", r.status_code)
        return None
    try:
        chunk = r.text.split("\n\n", 1)[1]
        arr = json.loads(chunk)
        res = json.loads(arr[0][2])
        u = res[1]
    except Exception as e:  # noqa
        log.info("gnews javobni o'qib bo'lmadi: %s", e)
        return None
    return u if isinstance(u, str) and u.startswith("http") else None
