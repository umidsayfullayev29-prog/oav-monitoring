"""Sinov: berilgan post matnini Gemini'ga berib, javobni ko'rsatadi (hech qayerga yubormaydi).
Ishlatish: tekshir.yml, mode = "ai:<post matni>" (rasm havolasi bo'lsa: "ai:<matn> ||| <rasm havolasi>")."""
import sys
import json
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from monitor import ai  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
arg = sys.argv[1] if len(sys.argv) > 1 else ""
text, _, img = arg.partition("|||")
text, img = text.strip(), img.strip() or None
res = ai.check_post(text, img)
model = ai._resolved[0] if ai._resolved else None
out = {"model": model, "match": res[0] if res else None, "reason": res[1] if res else None,
       "evidence": res[2] if res else None, "error": res is None}
print("GEMINI_TEST", json.dumps(out, ensure_ascii=False))
(ROOT / "data/peek.md").write_text("# Gemini sinovi\n\n```\n" + text + "\n```\n\n```json\n"
                                    + json.dumps(out, ensure_ascii=False, indent=1) + "\n```\n", "utf-8")
