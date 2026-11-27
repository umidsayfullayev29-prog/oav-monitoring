"""Sayt va o'z Telegram kanali bir xabarni bersa — "telegram_ustun" nashrlarida kanal posti yuboriladi."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor import main  # noqa: E402

SITE = "https://xushnudbek.uz/p/biznesni-royxatga-olish-savolnomasi-qanday-toldiriladi"
TG = {"url": "https://t.me/xushnudbek/35268", "layer": "Telegram", "outlet": "xushnudbek.uz", "lang": "uz",
      "links": [SITE]}
SITE_REC = {"url": SITE, "layer": "Saytlar", "outlet": "xushnudbek.uz", "lang": "uz"}


def _run(prefer):
    r = main.Run.__new__(main.Run)
    r.tg_prefer, r.found_keys = set(prefer), set()
    return r


def test_channel_post_kept_for_preferred_outlet():
    assert main.channel_url(dict(TG), _run(["xushnudbek.uz"])) == TG["url"]


def test_other_outlets_still_converted_to_site():
    r = dict(TG, url="https://t.me/daryo/1", outlet="daryo.uz",
             links=["https://daryo.uz/2026/11/27/biznesni-royxatga-olish-savolnomasi"])
    assert main.channel_url(r, _run(["xushnudbek.uz"])) == r["links"][0]


def test_group_choice_prefers_telegram():
    run = _run(["xushnudbek.uz"])
    opts = [dict(SITE_REC), dict(TG)]
    cand = min(opts, key=lambda x: (main.LANG_PRIORITY.get(x.get("lang"), 9),
                                    0 if run._tg_preferred(x) else 1, opts.index(x)))
    assert cand["url"] == TG["url"]
    # boshqa nashrda o'zgarmaydi (birinchisi)
    run2 = _run([])
    cand2 = min(opts, key=lambda x: (main.LANG_PRIORITY.get(x.get("lang"), 9),
                                     0 if run2._tg_preferred(x) else 1, opts.index(x)))
    assert cand2["url"] == SITE
