"""OAV monitoring: qo'mita haqidagi chiqishlarni topib, Telegram'ga yuboradi.

Ishga tushirish:  python -m monitor.main
Muhit o'zgaruvchilari: TG_BOT_TOKEN, TG_CHAT_ID (bo'lmasa xabarlar faqat logga chiqadi)
"""
import os
import re
import sys
import json
import time
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

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"      # GitHub Actions cache'da saqlanadi
DATA = ROOT / "data"        # repoga commit qilinadi
TZ = dt.timezone(dt.timedelta(hours=5))
NOW = dt.datetime.now(TZ)

RUN_BUDGET = int(os.environ.get("RUN_BUDGET_SEC", "200"))  # bitta ishga tushish uchun soniya
MAX_SEND = int(os.environ.get("MAX_SEND", "80"))
MAX_AGE_DAYS = 4          # bundan eski maqolalar yuborilmaydi
CHECKED_KEEP_DAYS = 21
EXCLUDE_HOSTS = {"stat.uz", "siat.stat.uz"}

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
        if self.found_path.exists():
            for line in self.found_path.read_text("utf-8").splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.found_keys.add(r.get("k"))
                if r.get("tk"):
                    self.found_keys.add(r["tk"])
        self.new_found = []
        self.msgs = load_json(STATE / "msgs.json", {})                 # k -> {mid, text, alts}
        self.alt_queue = [tuple(x) for x in self.meta.get("alt_carry", [])]  # (root_k, label, url)
        self.recent = []
        if self.found_path.exists():
            lim = (NOW - dt.timedelta(days=4)).isoformat()
            for line in self.found_path.read_text("utf-8").splitlines()[-3000:]:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (r.get("found_at") or "") >= lim and (r.get("nums") or r.get("img")):
                    self.recent.append(r)
        self.stats = {}   # source -> dict
        self.errors = {}

    # ------------------------------------------------------------ helpers
    def st(self, source):
        return self.stats.setdefault(source, {"links": 0, "feeds": 0, "checked": 0, "found": 0, "err": 0})

    def is_checked(self, url):
        return h(web.norm_url(url)) in self.checked

    def mark_checked(self, url):
        self.checked[h(web.norm_url(url))] = int(NOW.timestamp())

    def add_found(self, *, url, title, source, keyword, kind, snip, date, layer, img="", nums=None):
        nu = web.norm_url(url)
        k = h(nu)
        tk = title_key(title)
        if k in self.found_keys or (tk and tk in self.found_keys):
            return False
        if web.host_of(url) in EXCLUDE_HOSTS:
            return False
        self.found_keys.add(k)
        if tk:
            self.found_keys.add(tk)
        rec = {
            "k": k, "tk": tk, "url": url, "title": title, "source": source,
            "keyword": keyword, "kind": kind, "snippet": snip, "layer": layer,
            "date": date.astimezone(TZ).isoformat() if date else None,
            "found_at": NOW.isoformat(timespec="seconds"),
            "lang": ver.lang_label(url, title), "img": img or "", "nums": nums or [],
        }
        # Shu saytning boshqa tildagi versiyasi oldin topilganmi?
        if layer not in ("Google News",):
            for other in self.new_found + self.recent:
                if other["k"] != k and ver.same_story(rec, other):
                    root = other.get("alt_of") or other["k"]
                    rec["alt_of"] = root
                    self.alt_queue.append((root, rec["lang"], url))
                    break
        self.new_found.append(rec)
        if not rec.get("alt_of"):
            self.pending.append(rec)
        self.st(source)["found"] += 1
        log.info("TOPILDI [%s] %s — %s%s", source, keyword, title[:90],
                 "  (boshqa til versiyasi)" if rec.get("alt_of") else "")
        return True

    def too_old(self, date):
        return bool(date) and (NOW - date).days > MAX_AGE_DAYS

    # ------------------------------------------------------------ 1-qatlam: qidiruv
    def google_url(self, q, lang):
        regions = {"uz": ("uz", "UZ", "UZ:uz"), "ru": ("ru", "RU", "RU:ru"), "en": ("en-US", "US", "US:en"),
                   "tr": ("tr", "TR", "TR:tr"), "az": ("az", "AZ", "AZ:az"), "kk": ("kk", "KZ", "KZ:kk"),
                   "ky": ("ky", "KG", "KG:ky"), "tg": ("tg", "TJ", "TJ:tg"), "fr": ("fr", "FR", "FR:fr"),
                   "de": ("de", "DE", "DE:de"), "es": ("es", "ES", "ES:es"), "ar": ("ar", "AE", "AE:ar"),
                   "fa": ("fa", "IR", "IR:fa"), "zh": ("zh-CN", "CN", "CN:zh-Hans"), "ko": ("ko", "KR", "KR:ko"),
                   "ja": ("ja", "JP", "JP:ja")}
        hl, gl, ceid = regions.get(lang, ("en-US", "US", "US:en"))
        return ("https://news.google.com/rss/search?" +
                up.urlencode({"q": f"{q} when:3d", "hl": hl, "gl": gl, "ceid": ceid}))

    def bing_url(self, q, mkt):
        return "https://www.bing.com/news/search?" + up.urlencode({"q": q, "format": "rss", "mkt": mkt,
                                                                 "qft": 'sortbydate="1"'})

    def google_layer(self):
        """Google News — ketma-ket, oraliq bilan (aks holda 429 qaytaradi)."""
        s = self.st("google news")
        self.google_hits = []
        for it in self.src["search"].get("google_news", []):
            q = it["q"]
            url = self.google_url(q, it.get("lang", "en"))
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
        if len(cands) < 15:
            self._take_sitemaps(domain, cands)
        s["feeds"] = len(good_feeds)
        s["links"] = len(cands)
        if page_ok == 0 and not cands:
            return []
        new = [c for nu, c in cands.items() if h(nu) not in self.checked]
        new.sort(key=lambda c: (not self.kw.match(c.get("title", "")), c.get("date") is None))
        lim = self.src.get("max_new_articles_per_site", 40)
        if self.bootstrap or domain not in self.meta.get("sites_seen", []):
            lim = min(lim, 15)  # yangi sayt: birinchi marta ozroq, qolgani keyingi safarlarda
        out = []
        for c in new[:lim]:
            c["source"] = domain
            c["layer"] = "sayt"
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
                children = [x[0] for x in items if "news" in x[0].lower()][:2] or [x[0] for x in items[:2]]
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
        for loc, lm, title in recent[:150]:
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
        small = {c for c in filtered if c in subs and subs[c] is not None and subs[c] < min_subs and not recheck}
        chans += sorted(filtered - small)
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
            if c in filtered and n_subs is not None and n_subs < min_subs:
                continue
            posts = web.parse_tg(res[1])
            s["links"] += len(posts)
            for p in posts:
                if not p["text"] or (p["date"] and (NOW - p["date"]).total_seconds() > 36 * 3600):
                    continue  # Telegram'da faqat oxirgi 36 soat
                url = f"https://t.me/{p['post']}"
                if self.is_checked(url):
                    continue
                self.mark_checked(url)
                s["checked"] += 1
                first = p["text"].split("\n", 1)[0]
                m = self.kw.match(p["text"], title=first, trusted=True)
                if m:
                    out.append(dict(url=url, title=first[:150], source=f"t.me/{c}", keyword=m[1], kind=m[0],
                                    snip=snippet(p["text"], m[2], m[3]), date=p["date"], layer="Telegram",
                                    nums=ver.numbers(p["text"][:1500])))
        return out

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
        full = "\n".join(x for x in (title, art.get("desc") or c.get("desc") or "", art["text"]) if x)
        trusted = c["source"].endswith(".uz") or c["source"] in self.site_domains
        m = self.kw.match(full, title=title, trusted=trusted)
        if m:
            kind, kwd, a, b = m
            self.add_found(url=art.get("final") or c["url"], title=title, source=c["source"], keyword=kwd,
                           kind=kind, snip=snippet(full, a, b), date=date, layer=c["layer"],
                           img=ver.image_key(art.get("image") or ""),
                           nums=ver.numbers(title + " " + (art.get("desc") or "") + " " + art["text"][:1500]))

    # ------------------------------------------------------------ run
    def run(self):
        log.info("Boshlandi. bootstrap=%s, tekshirilgan=%d, navbatda=%d", self.bootstrap, len(self.checked),
                 len(self.pending))
        sites = [s for s in self.src.get("sites", []) if s.get("enabled", True)]
        self.site_domains = {s["domain"] for s in sites}
        # Ketma-ket 3 marta ochilmagan saytlar har 6-ishga tushishda bir tekshiriladi (vaqtni tejash)
        fails = self.meta.setdefault("fails", {})
        run_no = self.meta.get("run_no", 0) + 1
        self.meta["run_no"] = run_no
        skipped = [x["domain"] for x in sites if fails.get(x["domain"], 0) >= 3 and run_no % 6]
        sites = [x for x in sites if x["domain"] not in skipped]
        if skipped:
            log.info("Vaqtincha o'tkazildi (ochilmayotgan): %d ta sayt", len(skipped))
        self.fetch_pool = cf.ThreadPoolExecutor(64)
        site_pool = cf.ThreadPoolExecutor(32)
        aux_pool = cf.ThreadPoolExecutor(2)
        fut_google = aux_pool.submit(self.google_layer)
        fut_bing = aux_pool.submit(self.bing_layer)
        fut_tg = site_pool.submit(self.telegram_layer)
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
            for t in fut_tg.result(timeout=60):
                self.add_found(**t)
        except Exception as e:  # noqa
            self.errors["telegram"] = repr(e)[:150]
        # Google topgan maqola biz to'liq kuzatayotgan saytdan bo'lsa — o'tkazib yuboramiz
        # (sayt qatlami uni to'liq matn bilan topadi). Sayt ochilmagan bo'lsa — Google'dan olamiz.
        ok_domains = {d for d in self.site_domains if self.stats.get(d, {}).get("links")}
        for g in getattr(self, "google_hits", []):
            host = g.pop("host", "")
            if host and any(host == d or host.endswith("." + d) or d.endswith("." + host) for d in ok_domains):
                continue
            self.add_found(**g)
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
        self.persist_found()
        self.send()
        self.daily_summary()
        self.cleanup_and_save()
        self.write_summary()

    def persist_found(self):
        if not self.new_found:
            return
        DATA.mkdir(exist_ok=True)
        with self.found_path.open("a", encoding="utf-8") as f:
            for r in self.new_found:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ Telegram
    def send(self):
        tg = Telegram(os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID"))
        silent = NOW.hour >= 23 or NOW.hour < 7
        # boshqa til versiyalarini asosiy xabarga biriktirish
        alts = {}
        for root, lang, url in self.alt_queue:
            alts.setdefault(root, []).append((lang, url))
        self.pending.sort(key=lambda r: r.get("date") or r.get("found_at") or "")
        sent = 0
        rest = []
        for r in self.pending:
            if sent >= MAX_SEND:
                rest.append(r)
                continue
            extra = alts.pop(r["k"], [])
            text = format_message(r)
            mid = tg.send(text + alt_line(extra), silent=silent,
                          preview_url=r["url"] if r.get("layer") == "Telegram" else None)
            if mid:
                sent += 1
                self.msgs[r["k"]] = {"mid": mid if isinstance(mid, int) and mid > 0 else None, "text": text,
                                     "alts": extra, "ts": int(NOW.timestamp())}
            else:
                r["tries"] = r.get("tries", 0) + 1
                if r["tries"] < 5:
                    rest.append(r)
                    if extra:
                        alts[r["k"]] = extra
        # asosiy xabar oldin yuborilgan bo'lsa — uni tahrirlab, havola qo'shamiz
        edited = 0
        for root, extra in alts.items():
            m = self.msgs.get(root)
            pend = next((p for p in rest if p["k"] == root), None)
            if pend is not None:
                # asosiy xabar hali navbatda — keyingi safar birga ketadi
                self.alt_queue_carry = getattr(self, "alt_queue_carry", []) + [(root, l, u) for l, u in extra]
                continue
            if m and m.get("mid"):
                m["alts"] = (m.get("alts") or []) + extra
                if tg.edit(m["mid"], m["text"] + alt_line(m["alts"])):
                    edited += 1
                    continue
            # tahrirlab bo'lmasa — qisqa alohida xabar
            first = next((r for r in self.new_found if r.get("alt_of") == root), None)
            title = html.escape((first or {}).get("title", ""))
            tg.send(f"🌐 <b>Boshqa tilda ham chiqdi:</b> {title}" + alt_line(extra), silent=True,
                    reply_to=(m or {}).get("mid"))
        self.pending = rest
        self.sent = sent
        lim = NOW.timestamp() - 7 * 86400
        self.msgs = {k: v for k, v in self.msgs.items() if v.get("ts", 0) >= lim}
        log.info("Telegram: yuborildi %d, tahrirlandi %d, navbatda qoldi %d", sent, edited, len(rest))

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
        tg = Telegram(os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID"))
        if tg.send("\n".join(lines), silent=True, preview=False):
            self.meta["summary_day"] = today

    # ------------------------------------------------------------ saqlash
    def cleanup_and_save(self):
        lim = NOW.timestamp() - CHECKED_KEEP_DAYS * 86400
        self.checked = {k: v for k, v in self.checked.items() if v >= lim}
        STATE.mkdir(exist_ok=True)
        self.meta["alt_carry"] = getattr(self, "alt_queue_carry", [])
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
    return "\n🌐 Boshqa tillarda: " + " · ".join(parts)


LAYER_ICON = {"Google News": "🔎", "Bing News": "🔎", "sayt": "🗞", "Telegram": "✈️"}


def format_message(r):
    title = html.escape(r.get("title") or "(sarlavhasiz)")
    date = ""
    if r.get("date"):
        try:
            d = dt.datetime.fromisoformat(r["date"]).astimezone(TZ)
            date = " · " + d.strftime("%d.%m %H:%M")
        except ValueError:
            pass
    kind = " · <i>taxminiy</i>" if r.get("kind") == "taxminiy" else ""
    lines = [
        f"{LAYER_ICON.get(r.get('layer'), '🗞')} <b>{title}</b>",
        f"🌐 {html.escape(r.get('source') or '')}{lang_tag(r)}{date}",
    ]
    if kind:
        lines.append("🔎 <i>taxminiy</i>")
    if r.get("layer") == "Telegram":
        # birinchi qatorda — ochiq havola, tagida post ko'rinishi
        return "\n".join([html.escape(r["url"]), f"✈️ <b>{html.escape(r.get('source') or '')}</b>{date}"])
    if r.get("snippet") and r.get("kind") != "taxminiy" and r.get("layer") != "Telegram":
        lines.append("")
        lines.append(r["snippet"][:700])
    lines.append("")
    label = "Postni ochish" if r.get("layer") == "Telegram" else "Maqolani ochish"
    lines.append(f'<a href="{html.escape(r["url"], quote=True)}">{label}</a>')
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    Run().run()


if __name__ == "__main__":
    main()
