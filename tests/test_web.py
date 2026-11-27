"""Maqola matnini ajratish (web.extract_article): yon bloklar topilma bo'lib ketmasligi."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor import web  # noqa: E402

BODY = "Fanlar akademiyasi harbiy sohadagi 30 ta atamaning özbekça muqobillari röyxatini tasdiqladi. " * 6


def test_post_nav_block_cut():
    # xushnudbek.uz: maqoladan keyin "Şu postdan oldin" — oldingi post sarlavhasi bilan
    html = f"""<html><head><meta property="og:title" content="30 ta harbiy atamaning özbekça muqobili"></head>
    <body><h1>30 ta harbiy atamaning özbekça muqobili kiritilmoqda</h1><article>
    <p>{BODY}</p><p>Şevron —> Uqa 👉 @xushnudbek 👈 # atama # harbiy</p>
    <p>Xushnudbek.uz @xushnudbek · 696 ming obunaçi Şu postdan oldin 27-noyabr, 2026 , 17:04
    Biznesni röyxatga oliş savolnomasi qanday töldiriladi? 17,8 ming</p></article></body></html>"""
    a = web.extract_article(html)
    assert "atamaning" in a["text"]
    assert "Biznesni" not in a["text"]


def test_post_nav_cyrillic_and_sh():
    for marker in ("Шу постдан олдин", "Shu postdan keyin"):
        body = web._cut_post_nav(BODY + " " + marker + " Миллий статистика қўмитаси")
        assert "статистика" not in body and "atamaning" in body


def test_marker_at_start_not_cut():
    # belgi matn boshida bo'lsa (200 belgidan oldin) — matn o'chirilmaydi
    t = "Shu postdan oldin " + BODY
    assert web._cut_post_nav(t) == t
