"""Matnni normallashtirish va kalit so'zlarni topish."""
import re
import html as htmllib

APOS = set("'ʻʼ‘’`´ʹ′")
COMBINING_DOT = "̇"
# Yangi lotin yozuvi (Özbekiston, qömita, yaşaydi, ğ, ç) va turkiy harflar — odatiy o'zbek lotiniga
NEW_LATIN = {"ö": "o", "ő": "o", "ō": "o", "ŏ": "o", "ğ": "g", "ǵ": "g", "ş": "sh", "ș": "sh",
             "ç": "ch", "ı": "i", "ñ": "ng"}


def norm_with_map(s: str):
    """Kichik harf, tutuq belgilarisiz, bo'sh joylar siqilgan matn va
    har bir belgining asl matndagi o'rni (snippet uchun)."""
    out, idx = [], []
    prev_space = True
    for i, ch in enumerate(s):
        if ch in APOS or ch == COMBINING_DOT:
            continue
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            idx.append(i)
            prev_space = True
            continue
        if ch == "İ":
            low = "i"
        else:
            low = ch.lower().replace(COMBINING_DOT, "")
        if low == "ё":
            low = "е"
        low = "".join(NEW_LATIN.get(c, c) for c in low)
        for c in low:
            out.append(c)
            idx.append(i)
        prev_space = False
    return "".join(out), idx


def norm(s: str) -> str:
    return norm_with_map(s or "")[0]


def norm_pattern(p: str) -> str:
    p = "".join(ch for ch in p if ch not in APOS)
    p = p.replace("İ", "i").lower().replace(COMBINING_DOT, "").replace("ё", "е")
    return "".join(NEW_LATIN.get(c, c) for c in p)


class Matcher:
    """Kalit so'zlarni topadi.

    strong  — har doim hisoblanadi, faqat yonida (±60 belgi) BOSHQA davlat nomi
              bo'lsa va O'zbekiston nomi bo'lmasa — tashlab yuboriladi
              (masalan, "Tojikiston statistika agentligi").
    context — umumiy ibora: yonida (±120 belgi) yoki sarlavhada O'zbekiston nomi
              bo'lishi kerak; O'zbekiston saytlarida (trusted=True) matnning
              istalgan joyida O'zbekiston nomi bo'lsa yetarli.
    """

    def __init__(self, cfg: dict):
        comp = lambda lst: [re.compile(norm_pattern(p)) for p in (lst or [])]
        self.strong = comp(cfg.get("strong"))
        self.context = comp(cfg.get("context"))
        self.markers = [norm(m) for m in cfg.get("uzbekistan_markers", [])]
        fm = [re.escape(norm(m)) for m in cfg.get("foreign_markers", [])]
        self.foreign = re.compile(r"\b(?:" + "|".join(fm) + ")") if fm else None
        self.stat_words = [norm(w) for w in cfg.get("stat_words", [])]
        weak = cfg.get("weak") or {}
        self.weak = comp(weak.get("patterns")) if weak.get("enabled") else []

    def has_marker(self, t: str) -> bool:
        return any(m in t for m in self.markers)

    def _dist(self, t, s, e, w, finder, after=None):
        a = max(0, s - w)
        win = t[a: e + (w if after is None else after)]
        best = None
        for ms, me in finder(win):
            ms += a
            me += a
            d = 0 if (ms < e and me > s) else (s - me if me <= s else ms - e)
            best = d if best is None else min(best, d)
        return best

    def _foreign_near_sentence(self, t, s, e):
        """Faqat shu gap ichida, iboraga yopishgan holda (oldida 30, ortida 22 belgi)."""
        a = max(0, s - 30)
        cut = max(t.rfind(". ", a, s), t.rfind("! ", a, s), t.rfind("? ", a, s), t.rfind("\n", a, s))
        if cut >= 0:
            a = cut + 1
        b2 = e + 22
        stop = [x for x in (t.find(". ", e, b2), t.find("\n", e, b2)) if x >= 0]
        if stop:
            b2 = min(stop)
        return self._foreign_near(t[a:b2], s - a, e - a, s - a, b2 - e)

    def _foreign_near(self, t, s, e, before=45, after=22):
        """Ibora yonida (oldida 45, ortida 22 belgi) boshqa davlat nomi
        O'zbekiston nomidan yaqinroq turibdimi: "Tojikiston statistika agentligi",
        "комитет по статистике Кыргызстана", "Statistics Agency of Kazakhstan"."""
        if not self.foreign:
            return False
        df = self._dist(t, s, e, before, lambda x: [(m.start(), m.end()) for m in self.foreign.finditer(x)], after)
        if df is None:
            return False
        dm = self._dist(t, s, e, before, lambda x: [(m.start(), m.end()) for mk in self.markers
                                                    for m in re.finditer(re.escape(mk), x)], after)
        return dm is None or df < dm

    def match(self, text: str, title: str = "", trusted: bool = False):
        """(turi, topilgan ibora, boshlanish, tugash) — asl matn bo'yicha, yoki None."""
        t, idx = norm_with_map(text or "")
        if not t:
            return None
        found = None
        nt = norm(title) if title else ""
        title_foreign = bool(nt and self.foreign and self.foreign.search(nt) and not self.has_marker(nt))
        for p in self.strong:
            for m in p.finditer(t):
                if title_foreign and not self.has_marker(t[:m.start()]) \
                        and self._foreign_near(t, m.start(), m.end(), 250, 0):
                    continue  # maqola boshqa davlat haqida va ibora o'sha davlat idorasini bildiradi
                if not self._foreign_near_sentence(t, m.start(), m.end()):
                    found = ("aniq", m)
                    break
            if found:
                break
        if not found:
            title_has = self.has_marker(norm(title)) if title else False
            any_marker = trusted and self.has_marker(t)
            for p in self.context:
                for m in p.finditer(t):
                    s, e = m.start(), m.end()
                    if self._foreign_near(t, s, e):
                        continue  # "... Kazakhstan statistics agency" kabi
                    near = self.has_marker(t[max(0, s - 120): e + 120])
                    if near and not self._foreign_near(t, s, e, 120, 120):
                        found = ("aniq", m)
                        break
                    if (title_has or any_marker) and not self._foreign_near(t, s, e, 350, 200):
                        found = ("aniq", m)
                        break
                if found:
                    break
        if not found:
            for p in self.weak:
                m = p.search(t)
                if m:
                    found = ("umumiy", m)
                    break
        if not found:
            return None
        kind, m = found
        start = idx[m.start()]
        end = idx[m.end() - 1] + 1
        while end < len(text) and (text[end].isalnum() or text[end] in APOS):
            end += 1
        return kind, text[start:end], start, end


def snippet(text: str, start: int, end: int, width: int = 170) -> str:
    """Topilgan joy atrofidagi parcha, HTML uchun xavfsiz, topilgan so'z qalin."""
    a = max(0, start - width)
    b = min(len(text), end + width)
    if a > 0:
        sp = text.find(" ", a)
        if 0 <= sp < start:
            a = sp + 1
    if b < len(text):
        sp = text.rfind(" ", end, b)
        if sp > end:
            b = sp
    pre = htmllib.escape(text[a:start])
    mid = htmllib.escape(text[start:end])
    post = htmllib.escape(text[end:b])
    s = f"{'…' if a > 0 else ''}{pre}<b>{mid}</b>{post}{'…' if b < len(text) else ''}"
    return re.sub(r"\s+", " ", s).strip()
