import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from main import (
    FEISHU_SAFE_REQUEST_BYTES,
    build_feishu_cards,
    classify_source,
    demo_brief,
    extract_date_from_url_or_text,
    gen_feishu_sign,
    is_listing_page,
    normalize_url,
)


def test_normalize_url():
    assert normalize_url("HTTPS://Example.com/path/?utm_source=x#top") == "https://example.com/path"


def test_sign_is_stable():
    first = gen_feishu_sign(1700000000, "secret")
    second = gen_feishu_sign(1700000000, "secret")
    assert first == second
    assert len(base64.b64decode(first)) == 32


def test_listing_page_detection():
    assert is_listing_page("https://www.itsnicethat.com/graphic-design")
    assert is_listing_page("https://example.com/category/design")
    assert not is_listing_page("https://example.com/2026/07/18/a-specific-design-story")


def test_date_from_url_and_text():
    assert extract_date_from_url_or_text("https://example.com/2026/07/18/story", "", "") == "2026-07-18"
    assert extract_date_from_url_or_text("https://example.com/story", "Published July 17, 2026", "") == "2026-07-17"


def test_source_classification():
    official = classify_source("https://example.com/newsroom/new-product", "New product", "")
    assert official["source_tier_hint"] == "A"
    media = classify_source("https://www.dezeen.com/2026/07/18/story", "Story", "")
    assert media["source_tier_hint"] == "B"
    social = classify_source("https://www.instagram.com/p/abc", "Post", "")
    assert social["source_tier_hint"] == "C"


def test_cards_are_under_limit():
    cards = build_feishu_cards(demo_brief())
    assert cards
    for card in cards:
        payload = {"msg_type": "interactive", "card": card}
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        assert size < FEISHU_SAFE_REQUEST_BYTES
