"""Matnni normallashtirish va kalit so'zlarni topish."""
import re
import html as htmllib

APOS = set("'ʻʼ‘’`´ʹ′")
COMBINING_DOT = "̇"


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
        for c in low:
            out.append(c)
            idx.append(i)
        prev_space = False
    return "".join(out), idx


def norm(s: str) -> str:
    return norm_with_map(s or "")[0]


class Matcher:
    def __init__(self, cfg: dict):
        comp = lambda lst: [re.compile(norm_pattern(p)) for p in (lst or [])]
        self.strong = comp(cfg.get("strong"))
        self.context = comp(cfg.get("context"))
        self.markers = [norm(m) for m in cfg.get("uzbekistan_markers", [])]
        self.stat_words = [norm(w) for w in cfg.get("stat_words", [])]
        weak = cfg.get("weak") or {}
        self.weak = comp(weak.get("patterns")) if weak.get("enabled") else []

    def has_marker(self, t: str) -> bool:
        return any(m in t for m in self.markers)

    def match(self, text: str):
        """(turi, topilgan ibora, boshlanish, tugash) — asl matn bo'yicha."""
        t, idx = norm_with_map(text or "")
        if not t:
            return None
        found = None
        for p in self.strong:
            m = p.search(t)
            if m:
                found = ("aniq", m)
                break
        if not found and self.has_marker(t):
            for p in self.context:
                m = p.search(t)
                if m:
                    found = ("aniq", m)
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
        # so'zning oxirigacha kengaytirish: "qo'mita" -> "qo'mitasi"
        while end < len(text) and (text[end].isalnum() or text[end] in APOS):
            end += 1
        return kind, text[start:end], start, end

    def broad(self, title: str) -> bool:
        """Sarlavhada O'zbekiston + statistika so'zi bormi (taxminiy)."""
        t = norm(title)
        return self.has_marker(t) and any(w in t for w in self.stat_words)


def norm_pattern(p: str) -> str:
    # Iborani ham matn kabi normallashtiramiz, lekin regex belgilarini saqlaymiz.
    p = "".join(ch for ch in p if ch not in APOS)
    p = p.replace("İ", "i").lower().replace(COMBINING_DOT, "").replace("ё", "е")
    return p


def snippet(text: str, start: int, end: int, width: int = 170) -> str:
    """Topilgan joy atrofidagi parcha, HTML uchun xavfsiz, topilgan so'z qalin."""
    a = max(0, start - width)
    b = min(len(text), end + width)
    # so'z chegarasiga moslash
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
