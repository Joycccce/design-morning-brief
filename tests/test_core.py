import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from main import FEISHU_SAFE_REQUEST_BYTES, build_feishu_cards, demo_brief, gen_feishu_sign, normalize_url


def test_normalize_url():
    assert normalize_url("HTTPS://Example.com/path/?utm_source=x#top") == "https://example.com/path"


def test_sign_is_stable():
    first = gen_feishu_sign(1700000000, "secret")
    second = gen_feishu_sign(1700000000, "secret")
    assert first == second
    assert len(base64.b64decode(first)) == 32


def test_cards_are_under_limit():
    cards = build_feishu_cards(demo_brief())
    assert cards
    for card in cards:
        payload = {"msg_type": "interactive", "card": card}
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        assert size < FEISHU_SAFE_REQUEST_BYTES
