"""OAV monitoring: qo'mita haqidagi chiqishlarni topib, Telegram'ga yuboradi.

Ishga tushirish:  python -m monitor.main
Muhit o'zgaruvchilari: TG_BOT_TOKEN, TG_CHAT_ID (bo'lmasa xabarlar faqat logga chiqadi)
"""
import os
import re
import sys
import json
import time
import threading
import html
import hashlib
import logging
import datetime as dt
import urllib.parse as up
import concurrent.futures as cf
from pathlib import Path

import yaml
import feedparser

from .textutil import Matcher, snippet, norm
from . import web
from .telegram import Telegram
from . import versions as ver
from . import gnews
from . import ai

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"      # GitHub Actions cache'da saqlanadi
DATA = ROOT / "data"        # repoga commit qilinadi
TZ = dt.timezone(dt.timedelta(hours=5))
NOW = dt.datetime.now(TZ)

RUN_BUDGET = int(os.environ.get("RUN_BUDGET_SEC", "200"))  # bitta ishga tushish uchun soniya
MAX_SEND = int(os.environ.get("MAX_SEND", "80"))
MAX_AGE_DAYS = 4          # bundan eski maqolalar yuborilmaydi
# Qayta ko'rib chiqish rejimi: BACKFILL_SINCE=2026-11-01 — shu sanadan beri hamma manbalar qayta tekshiriladi
BACKFILL_SINCE = os.environ.get("BACKFILL_SINCE", "").strip()
BF_SINCE = dt.datetime.fromisoformat(BACKFILL_SINCE).replace(tzinfo=TZ) if BACKFILL_SINCE else None
CHANNEL_START = "2026-11-24T21:25"
BF_PART = os.environ.get("BF_PART", "").strip()          # sites:0/6 | tg:1/3 | search
BF_DEADLINE = int(os.environ.get("BF_DEADLINE_SEC", "17400"))   # shu vaqtdan beri topilmalar kanalga ketmoqda
if BF_SINCE:
    MAX_AGE_DAYS = (NOW - BF_SINCE).days + 1
    STATE = ROOT / "state_bf"
CHECKED_KEEP_DAYS = 21
EXCLUDE_HOSTS = {"stat.uz", "siat.stat.uz"}
# O'zimizning Telegram kanallar — OAV hisoblanmaydi
EXCLUDE_TG = {"statistikaoav", "statistika_rasmiy", "uzstataxborot", "census_2026", "census_2026_lotin",
              "uzstat", "uzstat_official", "stat_uz"}
ARCHIVE_CHAT = os.environ.get("ARCHIVE_CHAT", "").strip()   # topilmalar ketadigan kanal (@statistikaoav)
MAX_GNEWS_DECODE = 600 if BF_SINCE else 30
LANG_PRIORITY = {"uz": 0, "ўз": 1, "ru": 2, "en": 3}

log = logging.getLogger("oav")
T0 = time.monotonic()


def elapsed():
    return time.monotonic() - T0


def h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def title_key(title: str) -> str:
    t = norm(re.sub(r"\s+[-–—|]\s+[^-–—|]{2,60}$", "", title or ""))  # " - Manba" qismini olib tashlash
    t = re.sub(r"[^\w\s]", "", t)
    return h(" ".join(t.split()[:14])) if len(t) > 15 else ""


class Run:
    def __init__(self):
        self.kw = Matcher(yaml.safe_load((ROOT / "config/keywords.yml").read_text("utf-8")))
        self.src = yaml.safe_load((ROOT / "config/sources.yml").read_text("utf-8"))
        self.checked = load_json(STATE / "checked.json", {})          # url_hash -> ts
        self.feeds = load_json(STATE / "feeds.json", {})               # domain -> {feeds, ts}
        self.pending = load_json(STATE / "pending.json", [])
        self.meta = load_json(STATE / "meta.json", {})
        self.bootstrap = len(self.checked) == 0
        self.found_keys = set()
        self.found_path = DATA / "found.jsonl"
        if BF_SINCE:
            (DATA / "bf").mkdir(parents=True, exist_ok=True)
            self.found_path = DATA / "bf" / ((BF_PART or "all").replace(":", "_").replace("/", "of") + ".jsonl")
        self.recent = []
        lim4 = (NOW - dt.timedelta(days=4)).isoformat()
        for fname in ["found.jsonl"] + ([] if BF_SINCE else [str(x.relative_to(DATA)) for x in sorted((DATA / "bf").glob("*.jsonl"))] if (DATA / "bf").exists() else []):
            fp = DATA / fname
            if not fp.exists():
                continue
            for line in fp.read_text("utf-8").splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if BF_SINCE and fname == "found.jsonl":
                    # qayta ko'rishda: faqat kanalga allaqachon ketganlar "bor" hisoblanadi
                    fa = r.get("found_at") or ""
                    if fa < CHANNEL_START or r.get("kind") == "taxminiy" or "news.google." in (r.get("url") or ""):
                        continue
                    if r.get("nums") or r.get("img") or r.get("links"):
                        self.recent.append(r)
                self.found_keys.add(r.get("k"))
                if r.get("tk"):
                    self.found_keys.add(r["tk"])
                for x in r.get("xk") or []:
                    self.found_keys.add(x)
                if not BF_SINCE and (r.get("found_at") or "") >= lim4 and (r.get("nums") or r.get("img")):
                    self.recent.append(r)
        self.new_found = []
        self.tg_seen = set(self.meta.get("tg_seen", []))
        self.tg_seen_new = set()
        self.baseline_done = set(self.meta.get("baseline_done", []))
        self.baseline_new = set()
        self.msgs = load_json(STATE / "msgs.json", {})                 # k -> {mid, text, alts}
        self.alt_queue = [tuple(x) for x in self.meta.get("alt_carry", [])]  # (root_k, label, url)
        self.stats = {}   # source -> dict
        self.errors = {}

    # ------------------------------------------------------------ helpers
    def st(self, source):
        return self.stats.setdefault(source, {"links": 0, "feeds": 0, "checked": 0, "found": 0, "err": 0})

    def is_checked(self, url):
        return h(web.norm_url(url)) in self.checked

    def mark_checked(self, url):
        self.checked[h(web.norm_url(url))] = int(NOW.timestamp())

    def add_found(self, *, url, title, source, keyword, kind, snip, date, layer, img="", nums=None,
                  links=None, outlet=None, xk=None):
        nu = web.norm_url(url)
        k = h(nu)
        tk = title_key(title)
        if k in self.found_keys or (tk and tk in self.found_keys):
            return False
        if web.host_of(url) in EXCLUDE_HOSTS:
            return False
        # Havoladagi sana (/2026/07/25/) eski bo'lsa — eski maqola, sahifadagi sana noto'g'ri o'qilgan
        mu = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", url)
        if mu:
            try:
                ud = dt.datetime(int(mu.group(1)), int(mu.group(2)), int(mu.group(3)), tzinfo=TZ)
                if self.too_old(ud + dt.timedelta(hours=23)):
                    return False
                if not date or abs((date - ud).days) > 2:
                    date = ud
            except ValueError:
                pass
        if any(x in self.found_keys for x in (xk or [])):
            return False
        self.found_keys.add(k)
        for x in xk or []:
            self.found_keys.add(x)
        if tk:
            self.found_keys.add(tk)
        rec = {
            "k": k, "tk": tk, "url": url, "title": title, "source": source,
            "keyword": keyword, "kind": kind, "snippet": snip, "layer": layer,
            "date": date.astimezone(TZ).isoformat() if date else None,
            "found_at": NOW.isoformat(timespec="seconds"),
            "lang": ver.lang_label(url, title), "img": img or "", "nums": nums or [],
            "links": [web.norm_url(x) for x in (links or [])][:6],
            "outlet": outlet or ver.site_key(url),
        }
        if xk:
            rec["xk"] = list(xk)
        if layer == "Telegram":
            rec["lang"] = "tg"
        # Shu nashrning boshqa tildagi versiyasi yoki o'z Telegram kanalidagi posti oldin topilganmi?
        if "news.google." not in url:
            for other in self.new_found + self.recent:
                other.setdefault("outlet", ver.site_key(other["url"]))
                if other["k"] != k and ver.same_story(rec, other):
                    root = other.get("alt_of") or other["k"]
                    rec["alt_of"] = root
                    self.alt_queue.append((root, rec["lang"], url))
                    break
        self.new_found.append(rec)
        if BF_SINCE:
            with self.found_path.open("a", encoding="utf-8") as f:   # darhol yoziladi — to'xtab qolsa ham saqlanadi
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if not rec.get("alt_of"):
            self.pending.append(rec)
        self.st(source)["found"] += 1
        log.info("TOPILDI [%s] %s — %s%s", source, keyword, title[:90],
                 "  (boshqa til versiyasi)" if rec.get("alt_of") else "")
        return True

    def too_old(self, date):
        if BF_SINCE and date:
            return date < BF_SINCE
        return bool(date) and (NOW - date).days > MAX_AGE_DAYS

    # ------------------------------------------------------------ 1-qatlam: qidiruv
    def google_url(self, q, lang, window=None):
        regions = {"uz": ("uz", "UZ", "UZ:uz"), "ru": ("ru", "RU", "RU:ru"), "en": ("en-US", "US", "US:en"),
                   "tr": ("tr", "TR", "TR:tr"), "az": ("az", "AZ", "AZ:az"), "kk": ("kk", "KZ", "KZ:kk"),
                   "ky": ("ky", "KG", "KG:ky"), "tg": ("tg", "TJ", "TJ:tg"), "fr": ("fr", "FR", "FR:fr"),
                   "de": ("de", "DE", "DE:de"), "es": ("es", "ES", "ES:es"), "ar": ("ar", "AE", "AE:ar"),
                   "fa": ("fa", "IR", "IR:fa"), "zh": ("zh-CN", "CN", "CN:zh-Hans"), "ko": ("ko", "KR", "KR:ko"),
                   "ja": ("ja", "JP", "JP:ja")}
        hl, gl, ceid = regions.get(lang, ("en-US", "US", "US:en"))
        return ("https://news.google.com/rss/search?" +
                up.urlencode({"q": f"{q} after:{window[0]} before:{window[1]}" if window else f"{q} when:3d",
                              "hl": hl, "gl": gl, "ceid": ceid}))

    def bing_url(self, q, mkt):
        return "https://www.bing.com/news/search?" + up.urlencode({"q": q, "format": "rss", "mkt": mkt,
                                                                 "qft": 'sortbydate="1"'})

    def google_layer(self):
        """Google News — ketma-ket, oraliq bilan (aks holda 429 qaytaradi)."""
        s = self.st("google news")
        self.google_hits = []
        windows = [None]
        if BF_SINCE:
            windows, a = [], BF_SINCE.date()
            while a <= NOW.date():
                b = a + dt.timedelta(days=4)
                windows.append((a.isoformat(), b.isoformat()))
                a = b - dt.timedelta(days=1)
        jobs = [(it, w) for it in self.src["search"].get("google_news", []) for w in windows]
        for it, w in jobs:
            q = it["q"]
            url = self.google_url(q, it.get("lang", "en"), w)
            text = None
            for attempt in range(3):
                try:
                    _, text = web.fetch(url, 20)
                    break
                except Exception as e:  # noqa
                    if "429" in str(e) and attempt < 2:
                        time.sleep(15 * (attempt + 1))
                        continue
                    s["err"] += 1
                    self.errors[f"google: {q}"] = str(e)[:150]
                    break
            time.sleep(2.0)
            if text is None:
                continue
            fp = feedparser.parse(text)
            s["links"] += len(fp.entries)
            for e in fp.entries:
                title = html.unescape(e.get("title", ""))
                date = None
                if e.get("published_parsed"):
                    date = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
                if self.too_old(date):
                    continue
                src_name = (e.get("source") or {}).get("title") or "Google News"
                src_href = (e.get("source") or {}).get("href") or ""
                if web.host_of(src_href) in EXCLUDE_HOSTS:
                    continue
                desc = BeautifulText(e.get("summary", ""))
                for base in (title, desc):
                    mm = self.kw.match(base, title=title)
                    if mm:
                        self.google_hits.append(dict(url=e.get("link"), title=title, source=src_name,
                                                     keyword=mm[1], kind=mm[0],
                                                     snip=snippet(base, mm[2], mm[3]), date=date,
                                                     layer="Google News", host=web.host_of(src_href)))
                        break

    def bing_layer(self):
        """Bing News — haqiqiy havola beradi, maqola to'liq matni tekshiriladi."""
        s = self.st("bing news")
        out = []
        for it in self.src["search"].get("bing_news", []):
            q = it["q"]
            try:
                _, text = web.fetch(self.bing_url(q, it.get("mkt", "en-US")), 20)
            except Exception as e:  # noqa
                s["err"] += 1
                self.errors[f"bing: {q}"] = str(e)[:150]
                continue
            fp = feedparser.parse(text)
            s["links"] += len(fp.entries)
            for e in fp.entries:
                link = e.get("link", "")
                qs = up.parse_qs(up.urlsplit(link).query)
                real = qs.get("url", [link])[0]
                date = None
                if e.get("published_parsed"):
                    date = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
                if self.too_old(date) or web.host_of(real) in EXCLUDE_HOSTS or self.is_checked(real):
                    continue
                out.append({"url": real, "title": html.unescape(e.get("title", "")), "date": date,
                            "source": web.host_of(real), "layer": "Bing News",
                            "desc": BeautifulText(e.get("summary", ""))})
            time.sleep(0.5)
        return out

    # ------------------------------------------------------------ 2-3-qatlam: saytlar
    def _try(self, url, timeout=15):
        try:
            return web.fetch(url, timeout)
        except Exception as e:  # noqa
            return e

    def site_scan(self, site):
        """Bitta sayt: sahifalar + RSS + (kerak bo'lsa) sitemap -> tekshirilmagan maqolalar."""
        domain = site["domain"]
        s = self.st(domain)
        cands = {}
        feeds_found = set()
        cache = self.feeds.get(domain, {})
        page_urls = [f"https://{domain}{p}" for p in site.get("pages", ["/"])]
        probe = not cache or NOW.timestamp() - cache.get("ts", 0) > 7 * 86400
        feed_urls = set(cache.get("feeds", []))
        if probe:
            feed_urls |= {f"https://{domain}{p}" for p in
                          ("/rss", "/feed", "/rss.xml", "/feed.xml", "/ru/rss", "/uz/rss", "/oz/rss")}
        # sahifalar va RSS bir vaqtda
        futs = {self.fetch_pool.submit(self._try, u): ("page", u) for u in page_urls}
        futs.update({self.fetch_pool.submit(self._try, u, 12): ("feed", u) for u in feed_urls})
        good_feeds = []
        page_ok = 0
        for f in cf.as_completed(futs):
            kind, u = futs[f]
            res = f.result()
            if isinstance(res, Exception):
                if kind == "page":
                    s["err"] += 1
                    self.errors[u] = str(res)[:150]
                continue
            final, text = res
            if kind == "page":
                page_ok += 1
                links, feeds = web.extract_links(text, final, domain)
                feeds_found.update(x for x in feeds if "comment" not in x.lower())
                for lu, anchor in links:
                    cands.setdefault(web.norm_url(lu), {"url": lu, "title": anchor, "date": None})
            else:
                n = self._take_feed(text, domain, cands)
                if n:
                    good_feeds.append(u)
        # sahifada topilgan, lekin hali ko'rilmagan RSS
        extra = [u for u in feeds_found if u not in feed_urls][:4]
        for u in extra:
            res = self._try(u, 12)
            if not isinstance(res, Exception) and self._take_feed(res[1], domain, cands):
                good_feeds.append(u)
        if probe or good_feeds:
            self.feeds[domain] = {"feeds": sorted(set(good_feeds) | (set(cache.get("feeds", [])) if not probe else set())),
                                  "ts": int(NOW.timestamp()) if probe else cache.get("ts", 0),
                                  "sitemaps": cache.get("sitemaps")}
        # Havolalar kam bo'lsa (JS sayt yoki bloklangan sahifa) — sitemap
        if len(cands) < 15 or BF_SINCE:
            self._take_sitemaps(domain, cands)
        s["feeds"] = len(good_feeds)
        s["links"] = len(cands)
        if page_ok == 0 and not cands:
            return []
        new = [(nu, c) for nu, c in cands.items() if h(nu) not in self.checked]
        baseline = domain not in self.baseline_done and not BF_SINCE
        if baseline:
            # Saytni birinchi ko'rish: sanasi noma'lum havolalar (bosh sahifadagi eski "mashhur"
            # maqolalar, sitemap'dagi eski sahifalar) tarix deb belgilanadi va yuborilmaydi.
            # Keyin paydo bo'lgan har bir yangi havola — yangi maqola.
            for nu, c in new:
                if c.get("date") is None:
                    self.checked[h(nu)] = int(NOW.timestamp())
            new = [(nu, c) for nu, c in new if c.get("date") is not None]
            self.baseline_new.add(domain)
        new = [c for nu, c in new]
        new.sort(key=lambda c: (not self.kw.match(c.get("title", "")), c.get("date") is None))
        lim = 4000 if BF_SINCE else self.src.get("max_new_articles_per_site", 40)
        out = []
        for c in new[:lim]:
            c["source"] = domain
            c["layer"] = "sayt"
            c["baseline"] = baseline
            out.append(c)
        return out

    def _take_feed(self, text, domain, cands):
        fp = feedparser.parse(text)
        n = 0
        for e in fp.entries[:80]:
            link = e.get("link")
            if not link or not web.same_site(link, domain):
                continue
            n += 1
            date = None
            if e.get("published_parsed"):
                date = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
            if self.too_old(date):
                continue
            cands[web.norm_url(link)] = {"url": link, "title": html.unescape(e.get("title", "")),
                                         "date": date, "desc": BeautifulText(e.get("summary", ""))}
        return n

    def _take_sitemaps(self, domain, cands):
        cache = self.feeds.setdefault(domain, {"feeds": [], "ts": int(NOW.timestamp())})
        roots = cache.get("sitemaps")
        if roots is None:
            roots = []
            res = self._try(f"https://{domain}/robots.txt", 10)
            if not isinstance(res, Exception):
                roots = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", res[1])[:4]
            if not roots:
                roots = [f"https://{domain}/sitemap.xml", f"https://{domain}/news-sitemap.xml",
                         f"https://{domain}/sitemap-news.xml"]
            cache["sitemaps"] = roots
        good_roots = []
        for root in roots:
            res = self._try(root, 15)
            if isinstance(res, Exception):
                continue
            kind, items = web.parse_sitemap(res[1])
            if not items:
                continue
            good_roots.append(root)
            if kind == "index":
                items.sort(key=lambda x: (x[1] is not None, x[1] or NOW, x[0]), reverse=True)
                nch = 6 if BF_SINCE else 2
                children = [x[0] for x in items if "news" in x[0].lower()][:nch] or [x[0] for x in items[:nch]]
                for ch in children:
                    r2 = self._try(ch, 15)
                    if isinstance(r2, Exception):
                        continue
                    k2, urls = web.parse_sitemap(r2[1])
                    if k2 == "urlset":
                        self._add_sitemap_urls(urls, domain, cands)
            else:
                self._add_sitemap_urls(items, domain, cands)
        if good_roots:
            cache["sitemaps"] = good_roots

    def _add_sitemap_urls(self, urls, domain, cands):
        recent = [u for u in urls if u[1] and (NOW - u[1]).days <= MAX_AGE_DAYS]
        if not recent:
            recent = urls[-60:]  # sanasiz sitemap: oxirgilari
        for loc, lm, title in recent[:(5000 if BF_SINCE else 150)]:
            if web.same_site(loc, domain) and web.looks_like_article(loc):
                cands.setdefault(web.norm_url(loc), {"url": loc, "title": title, "date": lm})

    def telegram_layer(self):
        """Ochiq Telegram kanallari (t.me/s/<kanal> — login shart emas)."""
        norm_c = lambda lst: [c.strip().lstrip("@").lower() for c in (lst or [])]
        chans = norm_c(self.src.get("telegram_channels"))
        filtered = set(norm_c(self.src.get("telegram_channels_filtered"))) - set(chans)
        min_subs = int(self.src.get("telegram_channels_min_subscribers", 0) or 0)
        subs = self.meta.setdefault("tg_subs", {})
        # filtrlanadigan kanallar: obunachisi ma'lum va kam bo'lsa — o'tkazamiz
        # (har 50-ishga tushishda soni qayta tekshiriladi)
        recheck = self.meta.get("run_no", 0) % 50 == 0
        chans += sorted(filtered)
        # 5000+ obunachi talabi hamma kanallarga (asosiy ro'yxatga ham) qo'llanadi
        small = {c for c in chans if c in subs and subs[c] is not None and subs[c] < min_subs and not recheck}
        chans = [c for c in chans if c not in small]
        chans = [c for c in chans if c not in EXCLUDE_TG]
        if getattr(self, "tg_shard", None):
            i, n = self.tg_shard
            chans = [c for j, c in enumerate(chans) if j % n == i]
        ai_chans = set(norm_c(self.src.get("telegram_channels_ai")))
        self.ai_queue = []
        s = self.st("telegram")
        s["feeds"] = len(chans)
        out = []
        futs = {self.fetch_pool.submit(self._try, f"https://t.me/s/{c}", 15): c for c in chans}
        for f in cf.as_completed(futs):
            c = futs[f]
            res = f.result()
            if isinstance(res, Exception):
                s["err"] += 1
                self.errors[f"t.me/{c}"] = str(res)[:120]
                continue
            n_subs = web.tg_subscribers(res[1])
            subs[c] = n_subs
            if n_subs is not None and n_subs < min_subs:
                continue
            posts = web.parse_tg(res[1])
            # Faol kanal: sahifadagi postlarning hammasi yangi bo'lsa, orada qolib ketganlari bor —
            # oldingi sahifalarni ham o'qiymiz (ko'pi bilan 4 sahifa, 36 soat ichida).
            win = (NOW - BF_SINCE).total_seconds() if BF_SINCE else 36 * 3600
            if c in self.tg_seen or BF_SINCE:
                page = posts
                for _ in range(400 if BF_SINCE else 4):
                    fresh = [p for p in page if p["date"] and (NOW - p["date"]).total_seconds() <= win]
                    if not page or len(fresh) < len(page) or any(self.is_checked(f"https://t.me/{p['post']}") for p in page):
                        break
                    try:
                        before = min(int(p["post"].rsplit("/", 1)[1]) for p in page)
                    except ValueError:
                        break
                    r2 = self._try(f"https://t.me/s/{c}?before={before}", 15)
                    if isinstance(r2, Exception):
                        break
                    page = web.parse_tg(r2[1])
                    posts = page + posts
            self.tg_seen_new.add(c)
            # kanal qaysi saytniki: postlardagi havolalar bo'yicha o'rganamiz
            lk = self.meta.setdefault("tg_links", {}).setdefault(c, {})
            for p in posts:
                for u in p.get("links") or []:
                    d = ver.site_key(u)
                    if d and d != "t.me" and "." in d:
                        lk[d] = lk.get(d, 0) + 1
            if len(lk) > 30:
                self.meta["tg_links"][c] = dict(sorted(lk.items(), key=lambda x: -x[1])[:30])
            s["links"] += len(posts)
            for p in posts:
                if not (p["text"] or (c in ai_chans and p.get("img"))) or (p["date"] and (NOW - p["date"]).total_seconds() > win):
                    continue  # Telegram'da faqat oxirgi 36 soat (qayta ko'rishda — boshlang'ich sanadan)
                url = f"https://t.me/{p['post']}"
                if self.is_checked(url):
                    continue
                self.mark_checked(url)
                s["checked"] += 1
                first = p["text"].split("\n", 1)[0] if p["text"] else ""
                m = self.kw.match(p["text"], title=first, trusted=True) if p["text"] else None
                if not m and c in ai_chans and ai.available():
                    self.ai_queue.append({"c": c, "url": url, "text": p["text"], "img": p.get("img"),
                                          "date": p["date"].isoformat() if p["date"] else None,
                                          "links": p.get("links") or []})
                if m:
                    rec = dict(url=url, title=first[:150], source=f"t.me/{c}", keyword=m[1], kind=m[0],
                               snip=snippet(p["text"], m[2], m[3]), date=p["date"], layer="Telegram",
                               nums=ver.numbers(p["text"][:1500]), links=p.get("links") or [],
                               outlet=self.tg_outlet(c))
                    if BF_SINCE:
                        self.add_found(**rec)      # qayta ko'rishda darhol yoziladi
                    else:
                        out.append(rec)
        if BF_SINCE:
            self.ai_process(budget=3600, limit=10000)
        kept = sum(1 for c in filtered if (subs.get(c) or 0) >= min_subs)
        log.info("Telegram: %d ta kanal; statistikaoav ro'yxatidan %d/%d tasi %d+ obunachili",
                 len(chans), kept, len(filtered), min_subs)
        return out

    def ai_process(self, budget=90, limit=20):
        """Gemini tekshiruvi — asosiy monitoringdan alohida: vaqt va soni cheklangan, xato bo'lsa
        navbatda qoladi (keyingi safar qayta uriniladi), boshqa qatlamlarga ta'sir qilmaydi."""
        queue = self.meta.get("ai_pending", []) + getattr(self, "ai_queue", [])
        self.ai_queue = []
        if not queue or not ai.available():
            self.meta["ai_pending"] = queue[-150:]
            return
        dm_chans = set(c.lower() for c in (self.src.get("telegram_channels_ai_dm") or []))
        t0 = time.monotonic()
        rest = []
        for i, q in enumerate(queue):
            if i >= limit or time.monotonic() - t0 > budget:
                rest.append(q)
                continue
            try:
                res = ai.check_post(q["text"], q.get("img"), session=web.session())
            except Exception as e:  # noqa
                res = None
                self.errors["gemini"] = repr(e)[:150]
            self.st("gemini")["checked"] += 1
            if res is None:
                self.st("gemini")["err"] += 1
                q["tries"] = q.get("tries", 0) + 1
                if q["tries"] < 3:
                    rest.append(q)
                continue
            ok, reason, ev = res
            if not ok:
                continue
            self.st("gemini")["found"] += 1
            date = dt.datetime.fromisoformat(q["date"]) if q.get("date") else None
            first = ((q["text"] or "").split("\n", 1)[0] or ev or "Rasm")[:150]
            added = self.add_found(url=q["url"], title=first, source=f"t.me/{q['c']}", keyword="AI: " + reason[:60],
                                   kind="aniq", snip=html.escape(ev or reason), date=date, layer="Telegram",
                                   nums=ver.numbers((q["text"] or "")[:1500]), links=q.get("links") or [],
                                   outlet=self.tg_outlet(q["c"]))
            if added and q["c"] in dm_chans:
                self.new_found[-1]["notify_dm"] = True
                self.new_found[-1]["ai_reason"] = reason
        self.meta["ai_pending"] = rest[-150:]
        if queue:
            log.info("Gemini: %d ta post tekshirildi, navbatda %d", len(queue) - len(rest), len(rest))

    def tg_outlet(self, chan):
        """Kanal biror saytning rasmiy kanali bo'lsa — o'sha sayt domeni, aks holda t.me/<kanal>."""
        lk = self.meta.get("tg_links", {}).get(chan, {})
        tot = sum(lk.values())
        if lk and tot >= 5:
            d, n = max(lk.items(), key=lambda x: x[1])
            if n / tot >= 0.7:
                return d
        # nomidan, faqat aniq mos kelsa: kursivuz -> kursiv.media, zamonuz -> zamon.uz
        # ("daryouz_dunyo_yangiliklar" kabi boshqa kanallar mos kelmaydi)
        name = re.sub(r"[^a-z0-9]", "", chan.lower())
        for d in self.site_domains:
            parts = d.split(".")
            stem = parts[1] if parts[0] in ("uz", "oz", "ru", "en") and len(parts) > 2 else parts[0]
            stem = re.sub(r"[^a-z0-9]", "", stem)
            if len(stem) >= 4 and re.fullmatch(stem + r"(uz|ru|uzb|en|eng|official|news|media|rasmiy)?", name):
                return d
        return f"t.me/{chan}"

    def check_article(self, c):
        """Maqolani ochib, to'liq matnda qidiradi."""
        try:
            final, text = web.fetch(c["url"], 15)
        except Exception as e:  # noqa
            return c, None, str(e)[:150]
        try:
            art = web.extract_article(text)
        except Exception as e:  # noqa
            return c, None, "parse: " + str(e)[:120]
        art["final"] = final
        return c, art, None

    def handle_article(self, c, art):
        self.mark_checked(c["url"])
        if art.get("final"):
            self.mark_checked(art["final"])
        title = art["title"] or c.get("title") or ""
        date = art["date"] or c.get("date")
        if self.too_old(date):
            return
        if date is None and c.get("baseline"):
            return
        full = "\n".join(x for x in (title, art.get("desc") or c.get("desc") or "", art["text"]) if x)
        trusted = c["source"].endswith(".uz") or c["source"] in self.site_domains
        m = self.kw.match(full, title=title, trusted=trusted)
        if m:
            kind, kwd, a, b = m
            self.add_found(url=art.get("final") or c["url"], title=title, source=c["source"], keyword=kwd,
                           kind=kind, snip=snippet(full, a, b), date=date, layer=c["layer"],
                           img=ver.image_key(art.get("image") or ""), links=[art.get("final") or c["url"], c["url"]],
                           nums=ver.numbers(title + " " + (art.get("desc") or "") + " " + art["text"][:1500]))

    # ------------------------------------------------------------ run
    def run(self):
        # Tezlik: kunduzi (07–24) 30 daqiqada, kechasi soatda bir marta.
        # Budilnik yoki GitHub jadvali tez-tezroq chaqirsa ham, ortiqcha ishga tushish o'tkaziladi.
        gap = (55 if NOW.hour < 7 else 25) * 60
        last = self.meta.get("last_run_ts", 0)
        if not BF_SINCE and os.environ.get("FORCE_RUN") != "1" and NOW.timestamp() - last < gap:
            log.info("Oxirgi tekshiruvdan %d daqiqa o'tdi — bu safar o'tkazildi.", (NOW.timestamp() - last) // 60)
            return
        log.info("Boshlandi. bootstrap=%s, tekshirilgan=%d, navbatda=%d", self.bootstrap, len(self.checked),
                 len(self.pending))
        sites = [s for s in self.src.get("sites", []) if s.get("enabled", True)]
        self.site_domains = {s["domain"] for s in sites}
        # Ketma-ket 3 marta ochilmagan saytlar har 6-ishga tushishda bir tekshiriladi (vaqtni tejash)
        fails = self.meta.setdefault("fails", {})
        run_no = self.meta.get("run_no", 0) + 1
        self.meta["run_no"] = run_no
        skipped = [] if BF_SINCE else [x["domain"] for x in sites if fails.get(x["domain"], 0) >= 3 and run_no % 6]
        sites = [x for x in sites if x["domain"] not in skipped]
        if skipped:
            log.info("Vaqtincha o'tkazildi (ochilmayotgan): %d ta sayt", len(skipped))
        do_search = do_tg = True
        if BF_PART:
            kind, _, sh = BF_PART.partition(":")
            i, n = (int(x) for x in sh.split("/")) if sh else (0, 1)
            do_search = kind == "search"
            do_tg = kind == "tg"
            self.tg_shard = (i, n) if do_tg else None
            sites = [x for j, x in enumerate(sites) if kind == "sites" and j % n == i]
            log.info("Qism: %s — saytlar %d, telegram %s, qidiruv %s", BF_PART, len(sites), do_tg, do_search)
            _t = threading.Timer(BF_DEADLINE, lambda: (log.warning("Vaqt chegarasi — to'xtatildi"), os._exit(0)))
            _t.daemon = True     # ish erta tugasa, jarayon taymerni kutib o'tirmasin
            _t.start()
        self.fetch_pool = cf.ThreadPoolExecutor(64)
        site_pool = cf.ThreadPoolExecutor(32)
        aux_pool = cf.ThreadPoolExecutor(2)
        _none = cf.Future(); _none.set_result([])
        self.google_hits = []
        fut_google = aux_pool.submit(self.google_layer) if do_search else _none
        fut_bing = aux_pool.submit(self.bing_layer) if do_search else _none
        fut_tg = site_pool.submit(self.telegram_layer) if do_tg else _none
        site_futs = {site_pool.submit(self.site_scan, s): s["domain"] for s in sites}
        articles = []
        for f in cf.as_completed(site_futs):
            try:
                articles += f.result()
            except Exception as e:  # noqa
                self.errors[site_futs[f]] = "scan: " + repr(e)[:150]
        log.info("Saytlar ko'rildi (%.0fs). Yangi maqolalar: %d", elapsed(), len(articles))
        try:
            articles += fut_bing.result()
        except Exception as e:  # noqa
            self.errors["bing"] = repr(e)[:150]
        log.info("Bing tugadi (%.0fs). Jami tekshiriladigan maqolalar: %d", elapsed(), len(articles))

        art_futs = [self.fetch_pool.submit(self.check_article, c) for c in articles]
        stopped = False
        for f in cf.as_completed(art_futs):
            if f.cancelled():
                continue
            try:
                c, art, err = f.result()
            except Exception as e:  # noqa
                self.errors["article"] = repr(e)[:150]
                continue
            s = self.st(c["source"])
            if err:
                s["err"] += 1
                if c.get("layer") == "Bing News":
                    base = c["title"] + "\n" + c.get("desc", "")
                    mm = self.kw.match(base, title=c["title"])
                    if mm:
                        self.add_found(url=c["url"], title=c["title"], source=c["source"], keyword=mm[1],
                                       kind=mm[0], snip=snippet(base, mm[2], mm[3]), date=c.get("date"),
                                       layer="Bing News")
                    self.mark_checked(c["url"])
                continue
            s["checked"] += 1
            self.handle_article(c, art)
            if elapsed() > RUN_BUDGET and not stopped:
                stopped = True
                log.warning("Vaqt chegarasi — qolgan maqolalar keyingi safar tekshiriladi")
                for f2 in art_futs:
                    f2.cancel()
        try:
            fut_google.result(timeout=max(5, RUN_BUDGET + 60 - elapsed()))
        except Exception as e:  # noqa
            self.errors["google"] = repr(e)[:150]
        try:
            for t in fut_tg.result(timeout=(7200 if BF_SINCE else 60)):
                self.add_found(**t)
        except Exception as e:  # noqa
            self.errors["telegram"] = repr(e)[:150]
        if not BF_SINCE:
            try:
                self.ai_process()
            except Exception as e:  # noqa
                self.errors["gemini"] = repr(e)[:150]
        # Google topgan maqola biz to'liq kuzatayotgan saytdan bo'lsa — o'tkazib yuboramiz
        # (sayt qatlami uni to'liq matn bilan topadi). Sayt ochilmagan bo'lsa — Google'dan olamiz.
        ok_domains = {d for d in self.site_domains if self.stats.get(d, {}).get("links")}
        decoded = 0
        for g in getattr(self, "google_hits", []):
            host = g.pop("host", "")
            if host and any(host == d or host.endswith("." + d) or d.endswith("." + host) for d in ok_domains):
                continue
            gk = h(web.norm_url(g["url"]))
            tk = title_key(g["title"])
            if gk in self.found_keys or (tk and tk in self.found_keys):
                continue
            real = None
            if decoded < MAX_GNEWS_DECODE and elapsed() < RUN_BUDGET + 120:
                decoded += 1
                try:
                    real = gnews.decode(g["url"], session=web.session())
                except Exception as e:  # noqa
                    self.errors["gnews"] = repr(e)[:150]
                time.sleep(1.0)
            self.st("google news")["checked"] += 1
            if not real:
                self.add_found(**g)
                continue
            if web.host_of(real) in EXCLUDE_HOSTS:
                self.found_keys.add(gk)
                continue
            # Asl maqolani ochib, to'liq matnda tekshiramiz; ochilmasa — Google ma'lumoti bilan
            c = {"url": real, "title": g["title"], "source": g["source"], "layer": "Google News",
                 "date": g.get("date"), "desc": ""}
            _, art, err = self.check_article(c)
            before = len(self.new_found)
            if art and not err:
                title = art["title"] or g["title"]
                full = "\n".join(x for x in (title, art.get("desc") or "", art["text"]) if x)
                m = self.kw.match(full, title=title, trusted=True)
                self.mark_checked(real)
                if m:
                    self.add_found(url=art.get("final") or real, title=title, source=g["source"], keyword=m[1],
                                   kind=m[0], snip=snippet(full, m[2], m[3]), date=art["date"] or g.get("date"),
                                   layer="Google News", img=ver.image_key(art.get("image") or ""),
                                   links=[art.get("final") or real], xk=[gk] + ([tk] if tk else []),
                                   nums=ver.numbers(title + " " + art["text"][:1500]))
            if art and not err:
                if len(self.new_found) == before:
                    self.found_keys.add(gk)       # to'liq matnda tasdiqlanmadi — o'tkazamiz
            else:
                self.add_found(**dict(g, url=real, xk=[gk]))   # sayt ochilmadi (masalan, chetdan yopiq)
        if decoded:
            log.info("Google havolalari ochildi: %d ta urinish", decoded)
        self.fetch_pool.shutdown(wait=False, cancel_futures=True)
        site_pool.shutdown(wait=False, cancel_futures=True)
        aux_pool.shutdown(wait=False, cancel_futures=True)
        for x in sites:
            d = x["domain"]
            if self.stats.get(d, {}).get("links"):
                fails.pop(d, None)
            elif self.stats.get(d, {}).get("err"):
                fails[d] = fails.get(d, 0) + 1
        seen = set(self.meta.get("sites_seen", [])) | {d for d in self.site_domains if self.stats.get(d, {}).get("links")}
        self.meta["sites_seen"] = sorted(seen)
        log.info("Tekshiruv tugadi (%.0fs). Yangi topilmalar: %d", elapsed(), len(self.new_found))
        if BF_SINCE:
            self.persist_found()     # yuborish alohida qadamda (monitor.backfill_send)
            self.cleanup_and_save()
            return
        self.send()
        self.persist_found()
        self.daily_summary()
        self.cleanup_and_save()
        self.write_summary()

    def persist_found(self):
        if not self.new_found or BF_SINCE:
            return
        DATA.mkdir(exist_ok=True)
        with self.found_path.open("a", encoding="utf-8") as f:
            for r in self.new_found:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ Telegram
    def send(self):
        """Toza topilmalar — arxiv kanaliga (jadval o'zi ilib oladi), muammolilari — egasiga."""
        token = os.environ.get("TG_BOT_TOKEN")
        dm = Telegram(token, os.environ.get("TG_CHAT_ID"))
        ch = Telegram(token, ARCHIVE_CHAT) if ARCHIVE_CHAT else None
        silent = NOW.hour >= 23 or NOW.hour < 7
        # shu tekshiruvda topilgan boshqa til versiyalari (o'zbekchasiga ustunlik berish uchun)
        group = {}
        for r in self.new_found:
            if r.get("alt_of"):
                group.setdefault(r["alt_of"], []).append(r)
        self.pending.sort(key=lambda r: r.get("date") or r.get("found_at") or "")
        sent = to_dm = 0
        rest = []
        for r in self.pending:
            if sent + to_dm >= MAX_SEND:
                rest.append(r)
                continue
            cand = r
            opts = [r] + [a for a in group.get(r["k"], []) if "news.google." not in a["url"]]
            if len(opts) > 1:
                cand = min(opts, key=lambda x: (LANG_PRIORITY.get(x.get("lang"), 9), opts.index(x)))
                if cand is not r:
                    log.info("O'zbekcha versiya tanlandi: %s o'rniga %s", r["url"], cand["url"])
                    r["sent_as"] = cand["url"]
            why = problem_reason(cand)
            ok = False
            if not why and ch is not None:
                url = channel_url(cand, self)
                if url is None:      # shu maqola oldin yuborilgan (kanal posti sayt havolasiga olib boradi)
                    continue
                if url != cand["url"]:
                    cand.setdefault("xk", []).append(h(web.norm_url(url)))
                    self.found_keys.add(h(web.norm_url(url)))
                    cand["sent_as"] = url
                ok = bool(ch.send(channel_text(cand, url), silent=True, preview_url=url))
                if ok and cand.get("notify_dm"):
                    dm.send("🤖 <i>Gemini: " + html.escape(cand.get("ai_reason", "")) + "</i>\n" + format_message(cand),
                            silent=silent, preview_url=url)
                if ok:
                    sent += 1
                    time.sleep(1.5)
                    continue
                why = "Kanalga yuborib bo'lmadi (bot admin emasmi?)"
            text = (f"⚠️ <i>{html.escape(why)}</i>\n" if why else "") + format_message(cand)
            if dm.send(text, silent=silent, preview_url=cand["url"] if cand.get("layer") == "Telegram" else None):
                to_dm += 1
            else:
                r["tries"] = r.get("tries", 0) + 1
                if r["tries"] < 5:
                    rest.append(r)
        self.pending = rest
        self.sent = sent
        self.sent_dm = to_dm
        self.msgs = {}
        self.alt_queue = []
        log.info("Telegram: kanalga %d, egasiga %d, navbatda qoldi %d", sent, to_dm, len(rest))

    def daily_summary(self):
        today = NOW.strftime("%Y-%m-%d")
        if NOW.hour < 21 or self.meta.get("summary_day") == today:
            return
        day_recs = []
        if self.found_path.exists():
            for line in self.found_path.read_text("utf-8").splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (r.get("found_at") or "").startswith(today):
                    day_recs.append(r)
        by_src = {}
        for r in day_recs:
            by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        top = sorted(by_src.items(), key=lambda x: -x[1])[:15]
        lines = [f"📊 <b>Kunlik yakun — {NOW.strftime('%d.%m.%Y')}</b>",
                 f"Bugun topilgan chiqishlar: <b>{len(day_recs)}</b>", ""]
        lines += [f"• {html.escape(s)} — {n}" for s, n in top]
        lines += [""] + self.run_health_lines()
        tg = Telegram(os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID"))
        if tg.send("\n".join(lines), silent=True, preview=False):
            self.meta["summary_day"] = today

    def run_health_lines(self):
        """Bugungi tekshiruvlar soni va eng uzoq tanaffus (zanjir uzilganini ko'rish uchun)."""
        day0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        ts = sorted(set(self.meta.get("run_log", [])) | {int(NOW.timestamp())})
        prev = [t for t in ts if t < day0]
        today = [t for t in ts if t >= day0]
        seq = prev[-1:] + today
        gap, ga, gb = 0, None, None
        for a, b in zip(seq, seq[1:]):
            if b - a > gap:
                gap, ga, gb = b - a, a, b
        hm = lambda t: dt.datetime.fromtimestamp(t, TZ).strftime("%H:%M")  # noqa: E731
        out = [f"Bugungi tekshiruvlar: <b>{len(today)}</b> ta"]
        if gap > 90 * 60:
            out.append(f"⚠️ Eng uzoq tanaffus: <b>{gap // 3600} soat {gap % 3600 // 60} daq</b> ({hm(ga)}–{hm(gb)})")
        else:
            out.append("Tanaffus 1,5 soatdan oshmadi ✅")
        return out

    # ------------------------------------------------------------ saqlash
    def cleanup_and_save(self):
        lim = NOW.timestamp() - CHECKED_KEEP_DAYS * 86400
        self.checked = {k: v for k, v in self.checked.items() if v >= lim}
        STATE.mkdir(exist_ok=True)
        self.meta["alt_carry"] = getattr(self, "alt_queue_carry", [])
        self.meta["baseline_done"] = sorted(self.baseline_done | self.baseline_new)
        self.meta["tg_seen"] = sorted(self.tg_seen | self.tg_seen_new)
        self.meta["last_run_ts"] = int(NOW.timestamp())
        self.meta["run_log"] = (self.meta.get("run_log", []) + [int(NOW.timestamp())])[-150:]
        for name, obj in (("checked.json", self.checked), ("feeds.json", self.feeds),
                          ("pending.json", self.pending), ("meta.json", self.meta), ("msgs.json", self.msgs)):
            (STATE / name).write_text(json.dumps(obj, ensure_ascii=False, default=str), "utf-8")
        run = {"time": NOW.isoformat(timespec="seconds"), "seconds": round(elapsed()),
               "bootstrap": self.bootstrap, "new_found": len(self.new_found), "sent": getattr(self, "sent", 0),
               "pending": len(self.pending), "checked_total": len(self.checked),
               "sources": self.stats, "errors": dict(list(self.errors.items())[:80])}
        (STATE / "last_run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1, default=str), "utf-8")
        print("RUN_STATS " + json.dumps(run, ensure_ascii=False, default=str))

    def write_summary(self):
        p = os.environ.get("GITHUB_STEP_SUMMARY")
        rows = ["| Manba | Havolalar | RSS | Tekshirildi | Topildi | Xato |", "|---|---|---|---|---|---|"]
        for s, v in sorted(self.stats.items(), key=lambda x: (-x[1]["found"], x[0])):
            rows.append(f"| {s} | {v['links']} | {v['feeds']} | {v['checked']} | {v['found']} | {v['err']} |")
        txt = (f"## OAV monitoring — {NOW.strftime('%d.%m.%Y %H:%M')}\n\n"
               f"Yangi topilmalar: **{len(self.new_found)}**, yuborildi: **{getattr(self, 'sent', 0)}**, "
               f"navbatda: {len(self.pending)}, vaqt: {elapsed():.0f}s\n\n" + "\n".join(rows) + "\n")
        if p:
            with open(p, "a", encoding="utf-8") as f:
                f.write(txt)
        else:
            print(txt)


def BeautifulText(s: str) -> str:
    if not s:
        return ""
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(s)).split())


def lang_tag(r):
    lg = r.get("lang")
    return f" ({lg})" if lg and lg != "?" and r.get("layer") != "Google News" else ""


def alt_line(extra):
    if not extra:
        return ""
    seen, parts = set(), []
    for lg, url in extra:
        if url in seen:
            continue
        seen.add(url)
        parts.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(lg)}</a>')
    return "\n🌐 Boshqa versiyalari: " + " · ".join(parts)


URL_RE = re.compile(r"https?://\S+")


def problem_reason(r):
    if "news.google." in (r.get("url") or ""):
        return "Google havolasi: asl manzil aniqlanmadi — tekshiring"
    if r.get("kind") == "taxminiy":
        return "Taxminiy moslik — tekshiring"
    return None


def channel_url(r, run):
    """Rasmiy Telegram kanali o'z saytidagi maqolaga havola bersa — sayt havolasi yuboriladi."""
    if r.get("layer") != "Telegram":
        return r["url"]
    outlet = r.get("outlet") or ""
    if not outlet or outlet.startswith("t.me"):
        return r["url"]
    for u in r.get("links") or []:
        if ver.site_key(u) == outlet and web.looks_like_article(u):
            if h(web.norm_url(u)) in run.found_keys:
                return None
            return u
    return r["url"]


def _when(r):
    for key in ("date", "found_at"):
        if r.get(key):
            try:
                return dt.datetime.fromisoformat(r[key]).astimezone(TZ)
            except ValueError:
                pass
    return NOW


def channel_text(r, url):
    """Arxiv kanali uchun: havola, sarlavha, sana, iqtibos. Postda bitta havola bo'lishi shart."""
    d = _when(r)
    u = html.escape(url)
    if r.get("layer") == "Telegram":
        return f"{u}\n{d.strftime('%d.%m %H:%M')}"
    title = html.escape(URL_RE.sub("", r.get("title") or "").strip())
    lines = [u]
    if title:
        lines.append(f"📰 <b>{title}</b>")   # jadval birinchi qalin matnni sarlavha deb oladi
    lines.append(f"🗓 {d.strftime('%d.%m.%Y')}")
    # topilgan so'z qalin va tagi chizilgan; jadval sarlavhani postdagi birinchi qalin yozuvdan oladi —
    # sarlavha undan oldin turgani uchun iqtibosdagi qalin so'z sarlavha bo'lib ketmaydi
    snip = URL_RE.sub("", r.get("snippet") or "").replace("<b>", "<b><u>").replace("</b>", "</u></b>").strip()
    if snip:
        lines.append(f"<blockquote>{snip[:700]}</blockquote>")
    return "\n".join(lines)


LAYER_ICON = {"Google News": "🔎", "Bing News": "🔎", "sayt": "🗞", "Telegram": "✈️"}


def format_message(r):
    """Birinchi qatorda — ochiq havola, pastda sarlavha, manba, vaqt va matndan parcha."""
    title = html.escape(r.get("title") or "(sarlavhasiz)")
    date = ""
    if r.get("date"):
        try:
            d = dt.datetime.fromisoformat(r["date"]).astimezone(TZ)
            date = " · " + d.strftime("%d.%m %H:%M")
        except ValueError:
            pass
    lines = [html.escape(r["url"])]
    if r.get("layer") == "Telegram":
        lines.append(f"✈️ <b>{html.escape(r.get('source') or '')}</b>{date}")
        return "\n".join(lines)
    lines.append(f"{LAYER_ICON.get(r.get('layer'), '🗞')} <b>{title}</b>")
    lines.append(f"🌐 {html.escape(r.get('source') or '')}{lang_tag(r)}{date}")
    if r.get("kind") == "taxminiy":
        lines.append("🔎 <i>taxminiy</i>")
    elif r.get("snippet"):
        lines.append("")
        lines.append(r["snippet"][:700])
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    Run().run()


if __name__ == "__main__":
    main()
