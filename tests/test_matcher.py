"""Kalit so'z qoidalarini tekshirish: python -m pytest tests (yoki python tests/test_matcher.py)."""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor.textutil import Matcher  # noqa: E402

M = Matcher(yaml.safe_load((Path(__file__).resolve().parent.parent / "config/keywords.yml").read_text("utf-8")))

CASES = [
    # (matn, sarlavha, topilishi kerakmi)
    ("Oʻzbekiston Respublikasi Milliy statistika qoʻmitasi maʼlumotlariga koʻra, inflyatsiya pasaydi.", "", True),
    ("Миллий статистика қўмитасининг маълумотига кўра", "", True),
    ("По данным Нацкомстата", "", True),
    ("По данным комитета по статистике, в Ташкенте", "", True),
    ("According to the National Statistics Committee of Uzbekistan, exports rose", "", True),
    ("Milliy statistika qo'mitasi ma'lumotlariga ko'ra, Rossiyaga eksport oshdi", "Rossiyaga eksport oshdi", True),
    ("O'zbekistondan Rossiyaga eksport. Rossiya bilan savdo. Milliy statistika qo'mitasi ma'lumotlariga ko'ra, eksport oshdi",
     "Rossiyaga eksport oshdi", True),
    ("Тожикистон статистика агентлиги маълумотларига кўра", "", False),
    ("Qirgʻiziston Milliy statistika qoʻmitasi maʼlumotlariga koʻra", "", False),
    ("Over 36,000 Kazakh tourists visited Tajikistan. The Statistics Agency of Kazakhstan reported. Uzbekistan", "", False),
    ("Жители Кыргызстана. По данным Национального статистического комитета, население выросло. Узбекистан", "", False),
    ("Yana bir masala. Qirg‘iziston aholisi oshmoqda. Milliy statistika qo‘mitasi ma’lumotlariga ko‘ra, aholi 1,7 foizga ko‘paygan.",
     "Qirg‘iziston tog‘li qishloqlardagi onalarga katta pul", False),
    ("Türkiye İstatistik Kurumu verilerine göre", "", False),
    ("Lorem ipsum statistika", "", False),
    ("2026-yilda aholini roʻyxatga olish boshlanadi", "", True),
    ("Biznesni ro'yxatga olish jarayonida tadbirkorlar", "", True),
    ("Аҳоли ва қишлоқ хўжалигини рўйхатга олиш ўтказилади", "", True),
    ("Перепись населения в Узбекистане пройдет в 2026 году", "", True),
    ("Перепись населения в России показала", "Итоги переписи в России", False),
    ("Uzbekistan will hold its first population census in 2026", "", True),
    ("Qozog'istonda aholini ro'yxatga olish natijalari e'lon qilindi", "Qozog'iston aholisi", False),
    ("Opta статистика агентлиги маълумотларига кўра, Ҳусанов", "Гвардиола Ҳусанов ҳақида", False),
    ("Opta statistika agentligi maʼlumotlariga koʻra", "", False),
]


def test_cases():
    bad = []
    for text, title, expect in CASES:
        got = M.match(text, title=title, trusted=True) is not None
        if got != expect:
            bad.append((text[:70], expect, got))
    assert not bad, bad


if __name__ == "__main__":
    test_cases()
    print("OK", len(CASES))
