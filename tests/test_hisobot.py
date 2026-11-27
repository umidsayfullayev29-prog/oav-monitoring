"""Haftalik hisobot va /kunlik, /haftalik buyruqlari — soxta ma'lumot bilan."""
import sys
import json
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor import main  # noqa: E402

FRI = dt.datetime(2026, 11, 27, 18, 5, tzinfo=main.TZ)


def _rec(k, url, outlet, layer="sayt", days_ago=1, **kw):
    t = (FRI - dt.timedelta(days=days_ago)).isoformat()
    return dict(k=k, url=url, outlet=outlet, layer=layer, source=outlet, found_at=t, kind="aniq", **kw)


def _run(tmp_path, recs, monkeypatch, sent_keys=()):
    data = tmp_path / "data"
    data.mkdir()
    (data / "found.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", "utf-8")
    (data / "backfill_sent.txt").write_text("\n".join(sent_keys), "utf-8")
    monkeypatch.setattr(main, "DATA", data)
    r = main.Run.__new__(main.Run)
    r.found_path = data / "found.jsonl"
    r.meta = {"run_log": [int((FRI - dt.timedelta(hours=h / 2)).timestamp()) for h in range(0, 40)]}
    return r


RECS = [
    _rec("a1", "https://kun.uz/news/1", "kun.uz"),
    _rec("a2", "https://kun.uz/news/2", "kun.uz", days_ago=3),
    _rec("a3", "https://t.me/daryo/5", "daryo.uz", layer="Telegram", keyword="AI: qovun eksporti"),
    _rec("a4", "https://kun.uz/ru/news/1", "kun.uz", alt_of="a1"),            # nusxa — tashlangan
    _rec("a5", "https://news.google.com/x", "gazeta.uz", layer="Google News"),  # tekshirishga
    _rec("a6", "https://old.uz/1", "old.uz", days_ago=9),                      # haftadan tashqari
    _rec("a7", "https://rasmiy24.uz/1217", "rasmiy24.uz", alt_of="zz"),        # keyin qo'lda yuborilgan
]


def test_weekly_lines_counts_and_top(tmp_path, monkeypatch):
    run = _run(tmp_path, RECS, monkeypatch, sent_keys=["a7"])
    text = "\n".join(run.weekly_lines(FRI))
    assert "Kanalga ketgan chiqishlar: <b>4</b>" in text          # a1 a2 a3 a7
    assert "shundan Gemini topgan: 1" in text
    assert "Tekshirishga (sizga) yuborilgan: 1" in text
    assert "Boshqa til/nusxa sifatida tashlangan: 1" in text
    assert "1. kun.uz — 2" in text
    assert "old.uz" not in text


def test_weekly_only_on_friday_after_18(tmp_path, monkeypatch):
    run = _run(tmp_path, RECS, monkeypatch)
    sent = []
    monkeypatch.setattr(main.Telegram, "send", lambda self, text, **kw: sent.append(text) or 1)
    run.weekly_summary(FRI.replace(hour=17))
    run.weekly_summary(FRI - dt.timedelta(days=1))
    assert not sent
    run.weekly_summary(FRI)
    run.weekly_summary(FRI.replace(hour=19))          # shu kuni ikkinchi marta yo'q
    assert len(sent) == 1 and run.meta["weekly_day"] == "2026-11-27"


class FakeTG:
    token, chat_id = "x", "42"

    def __init__(self, batches):
        self.batches, self.sent, self.offsets = list(batches), [], []

    def updates(self, offset=None):
        self.offsets.append(offset)
        return (True, self.batches.pop(0), "") if self.batches else (True, [], "")

    def set_commands(self, cmds):
        return True

    def send(self, text, **kw):
        self.sent.append(text)
        return 1


def _msg(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_commands_only_from_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_CHAT_ID", "42")
    run = _run(tmp_path, RECS, monkeypatch, sent_keys=["a7"])
    run.daily_lines = lambda **kw: ["KUNLIK"]
    run.weekly_lines = lambda *a: ["HAFTALIK"]
    tg = FakeTG([[_msg(10, 7, "/haftalik"), _msg(11, 42, "/kunlik"), _msg(12, 42, "/kunlik@stat_oav_monitor_bot"),
                  {"update_id": 13, "channel_post": {"chat": {"id": -100}, "text": "x"}},
                  _msg(14, 42, "salom")]])
    run.handle_commands(tg)
    assert tg.sent == ["KUNLIK"]                      # begona chatdagi /haftalik e'tiborsiz, takror bir marta
    assert tg.offsets == [None, 15]                   # oxirgisi tasdiqlandi
    assert run.meta["cmds_set"] == 1


def test_webhook_conflict_warns_once(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_CHAT_ID", "42")
    run = _run(tmp_path, RECS, monkeypatch)
    tg = FakeTG([])
    tg.updates = lambda offset=None: (False, [], "409: Conflict: can't use getUpdates method while webhook is active")
    run.handle_commands(tg)
    run.handle_commands(tg)
    assert len(tg.sent) == 1 and "webhook" in tg.sent[0]
