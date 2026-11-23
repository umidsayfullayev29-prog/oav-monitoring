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

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"      # GitHub Actions cache'da saqlanadi
DATA = ROOT / "data"        # repoga commit qilinadi
TZ = dt.timezone(dt.timedelta(hours=5))
NOW = dt.datetime.now(TZ)

RUN_BUDGET = int(os.environ.get("RUN_BUDGET_SEC", "420"))  # bitta ishga tushish uchun soniya
MAX_SEND = int(os.environ.get("MAX_SEND", "80"))
MAX_AGE_DAYS = 4          # bundan eski maqolalar yuborilmaydi
CHECKED_KEEP_DAYS = 21
WORKERS = 32
EXCLUDE_HOSTS = {"stat.uz", "siat.stat.uz", "t.me"}

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
        self.stats = {}   # source -> dict
        self.errors = {}

    # ------------------------------------------------------------ helpers
    def st(self, source):
        return self.stats.setdefault(source, {"links": 0, "feeds": 0, "checked": 0, "found": 0, "err": 0})

    def is_checked(self, url):
        return h(web.norm_url(url)) in self.checked

    def mark_checked(self, url):
        self.checked[h(web.norm_url(url))] = int(NOW.timestamp())

    def add_found(self, *, url, title, source, keyword, kind, snip, date, layer):
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
        }
        self.new_found.append(rec)
        self.pending.append(rec)
        self.st(source)["found"] += 1
        log.info("TOPILDI [%s] %s — %s", source, keyword, title[:90])
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

    def search_layer(self, pool):
        jobs = []
        for it in self.src["search"].get("google_news", []):
            jobs.append(("google", it["q"], self.google_url(it["q"], it.get("lang", "en"))))
        for it in self.src["search"].get("bing_news", []):
            jobs.append(("bing", it["q"], self.bing_url(it["q"], it.get("mkt", "en-US"))))
        to_check = []
        futs = {pool.submit(web.fetch, u, 20): (eng, q, u) for eng, q, u in jobs}
        for f in cf.as_completed(futs):
            eng, q, u = futs[f]
            s = self.st(f"{eng} news")
            try:
                _, text = f.result()
            except Exception as e:  # noqa
                s["err"] += 1
                self.errors[f"{eng}: {q}"] = str(e)[:150]
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
                if eng == "google":
                    src_name = (e.get("source") or {}).get("title") or ""
                    src_href = (e.get("source") or {}).get("href") or ""
                    if web.host_of(src_href) in EXCLUDE_HOSTS:
                        continue
                    desc = BeautifulText(e.get("summary", ""))
                    m = self.kw.match(title) or self.kw.match(desc)
                    if m:
                        kind, kwd, a, b = m
                        base = title if self.kw.match(title) else desc
                        mm = self.kw.match(base)
                        self.add_found(url=e.get("link"), title=title, source=src_name or "Google News",
                                       keyword=kwd, kind=kind, snip=snippet(base, mm[2], mm[3]),
                                       date=date, layer="Google News")
                    elif self.kw.broad(title):
                        self.add_found(url=e.get("link"), title=title, source=src_name or "Google News",
                                       keyword=q, kind="taxminiy", snip=html.escape(title),
                                       date=date, layer="Google News")
                else:
                    link = e.get("link", "")
                    qs = up.parse_qs(up.urlsplit(link).query)
                    real = qs.get("url", [link])[0]
                    if web.host_of(real) in EXCLUDE_HOSTS or self.is_checked(real):
                        continue
                    to_check.append({"url": real, "title": title, "date": date,
                                     "source": web.host_of(real), "layer": "Bing News",
                                     "desc": BeautifulText(e.get("summary", "")), "query": q})
        return to_check

    # ------------------------------------------------------------ 2-3-qatlam: saytlar
    def site_scan(self, site):
        """Bitta sayt: sahifalar + RSS -> tekshirilmagan maqolalar ro'yxati."""
        domain = site["domain"]
        s = self.st(domain)
        cands = {}   # norm_url -> info
        feeds_found = set()
        for path in site.get("pages", ["/"]):
            url = f"https://{domain}{path}"
            try:
                final, text = web.fetch(url, 15)
            except Exception as e:  # noqa
                s["err"] += 1
                self.errors[url] = str(e)[:150]
                continue
            links, feeds = web.extract_links(text, final, domain)
            feeds_found.update(feeds)
            for u, anchor in links:
                cands.setdefault(web.norm_url(u), {"url": u, "title": anchor, "date": None})
        # RSS
        cache = self.feeds.get(domain, {})
        feed_urls = set(cache.get("feeds", [])) | {f for f in feeds_found if "comment" not in f.lower()}
        if not cache or NOW.timestamp() - cache.get("ts", 0) > 7 * 86400:
            for p in ("/rss", "/feed", "/rss.xml", "/feed.xml", "/ru/rss", "/uz/rss", "/oz/rss", "/en/rss"):
                feed_urls.add(f"https://{domain}{p}")
            probe = True
        else:
            probe = False
        good = []
        for fu in list(feed_urls)[:14]:
            try:
                _, text = web.fetch(fu, 12)
            except Exception:  # noqa
                continue
            fp = feedparser.parse(text)
            if not fp.entries:
                continue
            good.append(fu)
            for e in fp.entries[:60]:
                link = e.get("link")
                if not link or not web.same_site(link, domain):
                    continue
                date = None
                if e.get("published_parsed"):
                    date = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
                if self.too_old(date):
                    continue
                cands[web.norm_url(link)] = {"url": link, "title": html.unescape(e.get("title", "")),
                                             "date": date, "desc": BeautifulText(e.get("summary", ""))}
        if probe or good:
            self.feeds[domain] = {"feeds": sorted(set(good)), "ts": int(NOW.timestamp()) if probe else cache.get("ts", 0)}
        s["feeds"] = len(good)
        s["links"] = len(cands)
        new = [c for nu, c in cands.items() if h(nu) not in self.checked]
        # avval RSS (sanasi bor) va sarlavhasida kalit so'z borlari
        new.sort(key=lambda c: (not self.kw.match(c.get("title", "")), c.get("date") is None))
        lim = self.src.get("max_new_articles_per_site", 40)
        if self.bootstrap:
            lim = min(lim, 30)
        out = []
        for c in new[:lim]:
            c["source"] = domain
            c["layer"] = "sayt"
            out.append(c)
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
        m = self.kw.match(full)
        if m:
            kind, kwd, a, b = m
            self.add_found(url=art.get("final") or c["url"], title=title, source=c["source"], keyword=kwd,
                           kind=kind, snip=snippet(full, a, b), date=date, layer=c["layer"])

    # ------------------------------------------------------------ run
    def run(self):
        log.info("Boshlandi. bootstrap=%s, tekshirilgan=%d, navbatda=%d", self.bootstrap, len(self.checked),
                 len(self.pending))
        sites = [s for s in self.src.get("sites", []) if s.get("enabled", True)]
        with cf.ThreadPoolExecutor(WORKERS) as pool:
            fut_search = pool.submit(self.search_layer, pool)
            site_futs = {pool.submit(self.site_scan, s): s["domain"] for s in sites}
            articles = []
            for f in cf.as_completed(site_futs):
                try:
                    articles += f.result()
                except Exception as e:  # noqa
                    self.errors[site_futs[f]] = "scan: " + str(e)[:150]
            try:
                articles += fut_search.result()
            except Exception as e:  # noqa
                self.errors["search"] = str(e)[:150]
            log.info("Saytlar ko'rildi (%.0fs). Tekshiriladigan yangi maqolalar: %d", elapsed(), len(articles))

            art_futs = [pool.submit(self.check_article, c) for c in articles]
            for f in cf.as_completed(art_futs):
                c, art, err = f.result()
                s = self.st(c["source"])
                if err:
                    s["err"] += 1
                    # Bing natijasi ochilmasa ham sarlavhada so'z bo'lsa yuboramiz
                    if c.get("layer") == "Bing News":
                        mm = self.kw.match(c["title"] + "\n" + c.get("desc", ""))
                        if mm:
                            base = c["title"] + "\n" + c.get("desc", "")
                            self.add_found(url=c["url"], title=c["title"], source=c["source"], keyword=mm[1],
                                           kind=mm[0], snip=snippet(base, mm[2], mm[3]), date=c.get("date"),
                                           layer="Bing News")
                        self.mark_checked(c["url"])
                    continue
                s["checked"] += 1
                self.handle_article(c, art)
                if elapsed() > RUN_BUDGET:
                    log.warning("Vaqt chegarasi — qolgan maqolalar keyingi safar tekshiriladi")
                    for f2 in art_futs:
                        f2.cancel()
                    break
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
        # yangi topilmalar avval sanasi bo'yicha
        self.pending.sort(key=lambda r: r.get("date") or r.get("found_at") or "")
        sent = 0
        rest = []
        for r in self.pending:
            if sent >= MAX_SEND:
                rest.append(r)
                continue
            ok = tg.send(format_message(r), silent=silent)
            if ok:
                sent += 1
            else:
                r["tries"] = r.get("tries", 0) + 1
                if r["tries"] < 5:
                    rest.append(r)
        self.pending = rest
        self.sent = sent
        log.info("Telegram: yuborildi %d, navbatda qoldi %d", sent, len(rest))

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
        for name, obj in (("checked.json", self.checked), ("feeds.json", self.feeds),
                          ("pending.json", self.pending), ("meta.json", self.meta)):
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


LAYER_ICON = {"Google News": "🔎", "Bing News": "🔎", "sayt": "🗞"}


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
        f"🌐 {html.escape(r.get('source') or '')}{date}",
        f"🔑 {html.escape(r.get('keyword') or '')}{kind}",
    ]
    if r.get("snippet") and r.get("kind") != "taxminiy":
        lines.append("")
        lines.append(r["snippet"][:700])
    lines.append("")
    lines.append(f'<a href="{html.escape(r["url"], quote=True)}">Maqolani ochish</a>')
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    Run().run()


if __name__ == "__main__":
    main()
