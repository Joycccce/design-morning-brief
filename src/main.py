from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator
from tavily import TavilyClient


LOGGER = logging.getLogger("design-morning-brief")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
MIN_SCORE = int(os.getenv("MIN_SCORE", "72"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "10"))
FEISHU_SAFE_REQUEST_BYTES = 18_500

Category = Literal[
    "工业设计",
    "环境与空间设计",
    "创新设计",
    "社会与服务设计",
    "商业设计与战略",
    "视觉设计前沿",
    "设计教育与研究",
    "政策、奖项与机构",
]


class BriefItem(BaseModel):
    title_cn: str = Field(description="准确、克制的中文标题，不夸大")
    title_original: str = Field(description="来源原始标题；若无则与中文标题一致")
    category: Category
    source_name: str
    source_url: HttpUrl
    published_date: str = Field(description="YYYY-MM-DD；无法确认则写‘未知’")
    summary: str = Field(description="核心事实，60-110个中文字符")
    why_it_matters: str = Field(description="行业意义，40-90个中文字符")
    team_action: str = Field(description="团队可执行的观察或动作，30-70个中文字符")
    score: int = Field(ge=0, le=100)
    confidence: Literal["高", "中", "低"]

    @field_validator("title_cn", "title_original", "source_name", "published_date", "summary", "why_it_matters", "team_action")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class MorningBrief(BaseModel):
    report_date: str
    headline: str = Field(description="一句话概括今日设计行业最重要的变化")
    executive_summary: str = Field(description="100-180个中文字符，概括最重要的2-3个信号")
    items: list[BriefItem]
    visual_radar: list[str] = Field(description="3-5条视觉、海报、字体、品牌或生成式视觉趋势")
    follow_ups: list[str] = Field(description="2-4个值得团队继续追踪的问题")
    methodology_note: str = Field(description="说明搜索窗口、筛选阈值与局限，60-120个中文字符")


RECENT_QUERIES: list[tuple[str, int, str]] = [
    (
        "global industrial design product design manufacturing materials mobility consumer electronics design latest official announcement",
        2,
        "news",
    ),
    (
        "global architecture interior landscape exhibition retail experience environmental spatial design latest project research official",
        2,
        "news",
    ),
    (
        "innovation design design research service design systems design methods new report launch latest",
        2,
        "news",
    ),
    (
        "social design inclusive design accessibility civic design public interest community design latest initiative",
        2,
        "news",
    ),
    (
        "business design design strategy design consulting brand innovation customer experience organizational design latest",
        2,
        "news",
    ),
    (
        "top design schools design labs universities new research studio initiative industrial design architecture service design",
        2,
        "general",
    ),
    (
        "visual communication poster typography branding motion design generative design latest exhibition release",
        2,
        "news",
    ),
]

EXTENDED_QUERIES: list[tuple[str, int, str]] = [
    (
        "international design awards competition winners shortlist call for entries industrial architecture service social graphic design official",
        14,
        "general",
    ),
    (
        "design journal academic paper design research industrial design architecture service design social design newly published",
        14,
        "general",
    ),
    (
        "government design policy design council standard public innovation strategy report official latest",
        14,
        "general",
    ),
]

SOURCE_RUBRIC = """
价值评分总分100分：
- 来源权威性 25：官方机构、顶级院校、权威期刊、国际组织、获认可的设计/咨询机构优先。
- 战略影响 25：是否影响行业方向、方法、人才、商业模式、公共政策或供应链。
- 新颖性 15：是否提供真正的新事件、新研究或新实践，而非泛泛趋势文章。
- 跨领域相关性 15：是否能连接设计、咨询、商业、社会或技术。
- 团队可行动性 10：是否能转化为观察、研究、项目或内容选题。
- 证据质量 10：是否有明确事实、原始来源、时间与主体。
低于72分不保留；若合格信息不足，宁可少于8条，也不得编造或降低事实标准。
""".strip()

SYSTEM_INSTRUCTION = """
你是一名严谨的全球设计行业研究编辑，为设计、咨询与创新团队制作每日内部早报。
只能依据用户提供的候选来源工作，不得补写候选材料中不存在的事实、日期、机构、人物或结论。
优先原始来源、官方公告、专业期刊、顶级学院、政府与国际组织；对媒体转述保持克制。
遇到无法核实的发布时间，published_date 必须写“未知”。
合并同一事件的重复报道，并保留最原始、最权威的链接。
使用简体中文，表达专业、清晰、无营销腔。
""".strip()


def require_env(names: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = os.getenv(name, "").strip()
        if not value:
            missing.append(name)
        else:
            values[name] = value
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")
    return values


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        path = re.sub(r"/$", "", parts.path)
        tracking_prefixes = ("utm_", "gclid", "fbclid", "mc_cid", "mc_eid")
        clean_query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith(tracking_prefixes)
        ]
        return urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(clean_query, doseq=True), "")
        )
    except Exception:
        return url.strip()


def compact_text(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def domain_name(url: str) -> str:
    try:
        return urlsplit(url).netloc.removeprefix("www.")
    except Exception:
        return "未知来源"


def run_searches(api_key: str) -> tuple[list[dict], list[str], int]:
    client = TavilyClient(api_key=api_key)
    today = datetime.now(timezone.utc).date()
    errors: list[str] = []
    all_results: list[dict] = []
    credits_used = 0

    for query, days, topic in RECENT_QUERIES + EXTENDED_QUERIES:
        start_date = (today - timedelta(days=days)).isoformat()
        end_date = (today + timedelta(days=1)).isoformat()
        LOGGER.info("Tavily search: %s (window=%sd)", query[:70], days)
        try:
            response = client.search(
                query=query,
                search_depth="basic",
                topic=topic,
                max_results=5,
                start_date=start_date,
                end_date=end_date,
                include_answer=False,
                include_raw_content=False,
                include_images=False,
                include_favicon=False,
                include_usage=True,
                safe_search=True,
            )
            usage = response.get("usage") or {}
            credits_used += int(usage.get("credits") or 1)
            for result in response.get("results", []):
                if not result.get("url") or not result.get("title"):
                    continue
                item = {
                    "query": query,
                    "window_days": days,
                    "title": compact_text(result.get("title"), 240),
                    "url": result.get("url"),
                    "content": compact_text(result.get("content"), 1200),
                    "search_score": result.get("score"),
                    "published_date": result.get("published_date")
                    or result.get("publishedDate")
                    or "未知",
                }
                all_results.append(item)
        except Exception as exc:  # Continue so one weak source query does not kill the whole brief.
            message = f"{query[:55]}: {exc}"
            LOGGER.warning("Search failed: %s", message)
            errors.append(message)

    deduped: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for item in sorted(all_results, key=lambda x: float(x.get("search_score") or 0), reverse=True):
        normalized = normalize_url(str(item["url"]))
        title_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(item["title"]).lower())[:120]
        if normalized in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(normalized)
        seen_titles.add(title_key)
        item["url"] = normalized
        item["source_domain"] = domain_name(normalized)
        deduped.append(item)

    return deduped[:45], errors, credits_used


def build_analysis_prompt(candidates: list[dict], search_errors: list[str], credits_used: int) -> str:
    report_date = datetime.now(timezone.utc).date().isoformat()
    candidate_text = json.dumps(candidates, ensure_ascii=False, indent=2)
    return f"""
请基于下列候选来源制作 {report_date} 的全球设计行业早报。

覆盖范围：工业设计、环境与空间设计、创新设计、社会与服务设计、商业设计与战略、视觉设计前沿、设计教育与研究、政策/奖项/机构。
常规新闻窗口为最近48小时；期刊、奖项、政策、展览及重大机构报告窗口为最近14天。

筛选规则：
{SOURCE_RUBRIC}

输出要求：
1. 最终保留不超过 {MAX_ITEMS} 条，按 score 从高到低排序。
2. 每条必须使用候选材料中的真实链接；不得生成新链接。
3. 同一事件只留一条，优先原始和官方来源。
4. summary 只写事实；why_it_matters 写行业意义；team_action 写团队下一步。
5. headline 与 executive_summary 需要归纳跨条目的共同信号，不得凭空添加事实。
6. visual_radar 可以基于候选中的视觉、海报、品牌、字体和生成式设计信息总结；若证据不足，应写成“值得观察”而非确定结论。
7. methodology_note 必须写明：候选数量 {len(candidates)}、Tavily 约消耗 {credits_used} credits、筛选阈值 {MIN_SCORE}、搜索失败数 {len(search_errors)}。

候选来源 JSON：
{candidate_text}
""".strip()


def analyze_with_gemini(api_key: str, candidates: list[dict], search_errors: list[str], credits_used: int) -> MorningBrief:
    if len(candidates) < 5:
        raise RuntimeError(f"有效候选来源不足：仅 {len(candidates)} 条")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=DEFAULT_MODEL,
        contents=build_analysis_prompt(candidates, search_errors, credits_used),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=MorningBrief,
            max_output_tokens=10_000,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini 返回空内容")

    try:
        brief = MorningBrief.model_validate_json(response.text)
    except ValidationError as exc:
        raise RuntimeError(f"Gemini JSON 校验失败: {exc}") from exc

    eligible = [item for item in brief.items if item.score >= MIN_SCORE]
    eligible.sort(key=lambda item: item.score, reverse=True)
    brief.items = eligible[:MAX_ITEMS]
    if not brief.items:
        raise RuntimeError(f"没有达到 {MIN_SCORE} 分的新闻，停止发送以避免低质量早报")
    return brief


def markdown_element(content: str) -> dict:
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": "normal_v2",
        "margin": "0px 0px 8px 0px",
    }


def make_card(title: str, subtitle: str, summary: str, elements: list[dict], template: str = "blue") -> dict:
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "summary": {"content": compact_text(summary, 180)},
        },
        "header": {
            "title": {"tag": "plain_text", "content": compact_text(title, 80)},
            "subtitle": {"tag": "plain_text", "content": compact_text(subtitle, 80)},
            "template": template,
            "padding": "12px 12px 12px 12px",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
    }


def safe_link(url: str) -> str:
    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        return ""
    return url.replace("'", "%27")


def build_feishu_cards(brief: MorningBrief) -> list[dict]:
    cards: list[dict] = []
    total_item_cards = (len(brief.items) + 2) // 3
    total_cards = 2 + total_item_cards

    intro_elements = [
        markdown_element(f"**今日核心判断**\n{compact_text(brief.headline, 180)}"),
        markdown_element(f"**执行摘要**\n{compact_text(brief.executive_summary, 360)}"),
        markdown_element(
            f"**筛选结果**\n共保留 **{len(brief.items)}** 条高价值信息，最低分 {min(i.score for i in brief.items)}，按战略价值排序。"
        ),
    ]
    cards.append(
        make_card(
            f"设计行业早报 · {brief.report_date}",
            f"全球设计、咨询与战略情报 · 1/{total_cards}",
            brief.headline,
            intro_elements,
            "blue",
        )
    )

    for chunk_index in range(0, len(brief.items), 3):
        chunk = brief.items[chunk_index : chunk_index + 3]
        elements: list[dict] = []
        for offset, item in enumerate(chunk, start=chunk_index + 1):
            link = safe_link(str(item.source_url))
            source_line = f"{compact_text(item.source_name, 45)} · {compact_text(item.published_date, 12)} · 可信度{item.confidence}"
            content = (
                f"**{offset}. [{item.category}] {compact_text(item.title_cn, 86)}**  `{item.score}/100`\n"
                f"{compact_text(item.summary, 220)}\n"
                f"**为什么重要：** {compact_text(item.why_it_matters, 180)}\n"
                f"**团队动作：** {compact_text(item.team_action, 150)}\n"
                f"{source_line} · <a href='{link}'>查看原文</a>"
            )
            elements.append(markdown_element(content))
        card_number = 2 + chunk_index // 3
        cards.append(
            make_card(
                "高价值设计情报",
                f"按价值评分排序 · {card_number}/{total_cards}",
                f"设计行业早报重点情报第 {chunk_index + 1} 至 {chunk_index + len(chunk)} 条",
                elements,
                "turquoise",
            )
        )

    radar_lines = "\n".join(f"- {compact_text(x, 150)}" for x in brief.visual_radar[:5]) or "- 本期证据不足，暂不形成视觉趋势判断。"
    follow_lines = "\n".join(f"- {compact_text(x, 150)}" for x in brief.follow_ups[:4]) or "- 暂无。"
    end_elements = [
        markdown_element(f"**视觉与海报趋势雷达**\n{radar_lines}"),
        markdown_element(f"**建议继续追踪**\n{follow_lines}"),
        markdown_element(f"**方法说明**\n{compact_text(brief.methodology_note, 320)}"),
    ]
    cards.append(
        make_card(
            "视觉趋势与后续追踪",
            f"早报收尾 · {total_cards}/{total_cards}",
            "视觉趋势雷达、团队追踪问题与方法说明",
            end_elements,
            "purple",
        )
    )

    for index, card in enumerate(cards, start=1):
        payload = {"msg_type": "interactive", "card": card}
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > FEISHU_SAFE_REQUEST_BYTES:
            raise RuntimeError(f"第 {index} 张卡片请求体 {size} bytes，超过安全上限 {FEISHU_SAFE_REQUEST_BYTES}")
    return cards


def gen_feishu_sign(timestamp: int, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_card(webhook_url: str, card: dict, secret: str | None = None) -> dict:
    payload: dict = {"msg_type": "interactive", "card": card}
    if secret:
        timestamp = int(time.time())
        payload["timestamp"] = timestamp
        payload["sign"] = gen_feishu_sign(timestamp, secret)

    response = requests.post(
        webhook_url,
        json=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"飞书返回非 JSON: {response.text[:300]}") from exc

    code = result.get("code", result.get("StatusCode", 0))
    if code not in (0, "0", None):
        raise RuntimeError(f"飞书发送失败: {result}")
    return result


def save_outputs(brief: MorningBrief, candidates: list[dict], cards: list[dict], search_errors: list[str]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    (OUTPUT_DIR / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "search-errors.json").write_text(json.dumps(search_errors, ensure_ascii=False, indent=2), encoding="utf-8")
    for index, card in enumerate(cards, start=1):
        payload = {"msg_type": "interactive", "card": card}
        (OUTPUT_DIR / f"card-{index}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def demo_brief() -> MorningBrief:
    today = datetime.now(timezone.utc).date().isoformat()
    return MorningBrief(
        report_date=today,
        headline="这是连通性测试卡片，不包含真实新闻。",
        executive_summary="此消息用于验证 GitHub Actions、签名校验与飞书 Webhook 是否连接成功。真实早报会在完整运行模式下调用 Tavily 与 Gemini。",
        items=[
            BriefItem(
                title_cn="飞书消息卡片连接测试",
                title_original="Feishu card connectivity test",
                category="政策、奖项与机构",
                source_name="Feishu Open Platform",
                source_url="https://open.feishu.cn/",
                published_date=today,
                summary="程序已经成功生成符合飞书卡片结构的测试内容，并准备通过自定义机器人 Webhook 发送。",
                why_it_matters="这证明云端运行环境能够安全读取仓库 Secrets 并与飞书机器人通信。",
                team_action="确认群聊收到卡片后，再运行完整的新闻搜索与分析流程。",
                score=100,
                confidence="高",
            )
        ],
        visual_radar=["测试模式不形成视觉趋势判断。"],
        follow_ups=["检查卡片是否完整显示。", "确认链接能正常打开。"],
        methodology_note="测试模式：未调用 Tavily 与 Gemini，不消耗搜索或模型额度。",
    )


def run_full(send: bool = True) -> None:
    secrets = require_env(["TAVILY_API_KEY", "GEMINI_API_KEY"])
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    webhook_secret = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip() or None
    if send and not webhook_url:
        raise RuntimeError("发送模式缺少 FEISHU_WEBHOOK_URL")

    candidates, search_errors, credits_used = run_searches(secrets["TAVILY_API_KEY"])
    LOGGER.info("Deduplicated candidates: %s; Tavily credits: %s", len(candidates), credits_used)
    brief = analyze_with_gemini(secrets["GEMINI_API_KEY"], candidates, search_errors, credits_used)
    cards = build_feishu_cards(brief)
    save_outputs(brief, candidates, cards, search_errors)

    if send:
        for index, card in enumerate(cards, start=1):
            send_card(webhook_url, card, webhook_secret)
            LOGGER.info("Sent Feishu card %s/%s", index, len(cards))
            time.sleep(0.6)
    else:
        LOGGER.info("Dry run completed; no Feishu message sent")


def run_test_card() -> None:
    secrets = require_env(["FEISHU_WEBHOOK_URL"])
    webhook_secret = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip() or None
    brief = demo_brief()
    cards = build_feishu_cards(brief)
    save_outputs(brief, [], cards, [])
    for index, card in enumerate(cards, start=1):
        send_card(secrets["FEISHU_WEBHOOK_URL"], card, webhook_secret)
        LOGGER.info("Sent test card %s/%s", index, len(cards))
        time.sleep(0.6)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="自动生成并发送全球设计行业早报")
    parser.add_argument(
        "--mode",
        choices=["full", "dry-run", "test-feishu"],
        default="full",
        help="full=完整运行并发送；dry-run=生成文件但不发送；test-feishu=只测试飞书连接",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    try:
        if args.mode == "full":
            run_full(send=True)
        elif args.mode == "dry-run":
            run_full(send=False)
        else:
            run_test_card()
        return 0
    except Exception as exc:
        LOGGER.exception("Morning brief failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
