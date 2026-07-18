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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator
from tavily import TavilyClient


LOGGER = logging.getLogger("design-morning-brief")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
REPORT_TIMEZONE = os.getenv("REPORT_TIMEZONE", "Asia/Singapore")
MIN_SCORE = int(os.getenv("MIN_SCORE", "72"))
MIN_SEND_ITEMS = int(os.getenv("MIN_SEND_ITEMS", "3"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "10"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "50"))
MAX_PRIMARY_SOURCE_LOOKUPS = int(os.getenv("MAX_PRIMARY_SOURCE_LOOKUPS", "3"))
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

SourceTier = Literal["A", "B", "C"]
SourceType = Literal[
    "政府或国际组织",
    "学院或研究机构",
    "奖项官方机构",
    "专业机构",
    "公司官方公告",
    "学术期刊或论文库",
    "专业媒体",
    "新闻稿转载",
    "社交媒体",
    "其他来源",
]


class BriefItem(BaseModel):
    title_cn: str = Field(description="准确、克制的中文标题，不夸大")
    title_original: str = Field(description="来源原始标题；若无则与中文标题一致")
    category: Category
    source_name: str
    source_type: SourceType = "其他来源"
    source_tier: SourceTier = "C"
    is_primary_source: bool = False
    source_url: HttpUrl
    published_date: str = Field(description="YYYY-MM-DD；正式入选条目不得为未知")
    summary: str = Field(description="核心事实，60-110个中文字符，保留计划、预计、最高可达等限定词")
    why_it_matters: str = Field(description="行业意义，40-90个中文字符")
    team_action: str = Field(description="团队可执行的观察或动作，30-70个中文字符")
    score: int = Field(ge=0, le=100)
    confidence: Literal["高", "中", "低"]

    @field_validator(
        "title_cn",
        "title_original",
        "source_name",
        "published_date",
        "summary",
        "why_it_matters",
        "team_action",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class VisualSignal(BaseModel):
    signal: str = Field(description="克制的视觉趋势信号，不得写成无证据的行业定论")
    evidence: str = Field(description="候选来源中能支持该信号的具体事实")
    source_name: str
    source_url: HttpUrl
    published_date: str = Field(description="YYYY-MM-DD")
    confidence: Literal["高", "中", "低"]

    @field_validator("signal", "evidence", "source_name", "published_date")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class MorningBrief(BaseModel):
    report_date: str
    headline: str = Field(description="一句话概括今日设计行业最重要的变化")
    executive_summary: str = Field(description="100-180个中文字符，概括最重要的2-3个信号")
    items: list[BriefItem]
    visual_radar: list[VisualSignal] = Field(
        description="0-5条有明确来源的视觉、海报、字体、品牌或生成式视觉趋势；证据不足时返回空数组"
    )
    follow_ups: list[str] = Field(description="2-4个值得团队继续追踪的问题")
    methodology_note: str = Field(description="模型可填写，但程序会覆盖为审慎的方法说明")


@dataclass(frozen=True)
class QuerySpec:
    name: str
    query: str
    window_days: int
    topic: Literal["general", "news"]
    category_hint: Category
    include_domains: tuple[str, ...] = ()


SOCIAL_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "pinterest.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "youtube.com",
}

LOW_QUALITY_DOMAINS = {
    "medium.com",
    "substack.com",
    "reddit.com",
    "quora.com",
}

PRESS_RELEASE_DOMAINS = {
    "businesswire.com",
    "globenewswire.com",
    "prnewswire.com",
    "accesswire.com",
    "einpresswire.com",
    "markets.ft.com",
}

AWARD_DOMAINS = {
    "ifdesign.com",
    "red-dot.org",
    "bigsee.eu",
    "designboom.com/competition",
    "dezeen.com/awards",
    "core77.com/awards",
    "aia.org",
}

OFFICIAL_ORG_DOMAINS = {
    "designcouncil.org.uk",
    "wdo.org",
    "aiga.org",
    "ico-d.org",
    "ixda.org",
    "service-design-network.org",
    "cooperhewitt.org",
    "moma.org",
    "posterhouse.org",
}

ACADEMIC_DOMAINS = {
    "tandfonline.com",
    "sciencedirect.com",
    "springer.com",
    "cambridge.org",
    "mitpressjournals.org",
    "dl.acm.org",
    "arxiv.org",
    "ijdesign.org",
    "designsociety.org",
}

PROFESSIONAL_MEDIA_DOMAINS = {
    "dezeen.com",
    "designboom.com",
    "core77.com",
    "fastcompany.com",
    "itsnicethat.com",
    "creativeboom.com",
    "designweek.co.uk",
    "creativebloq.com",
    "architecturalrecord.com",
    "archdaily.com",
    "thearchitectsnewspaper.com",
    "retailgazette.co.uk",
    "adage.com",
    "roboticsandautomationnews.com",
    "thelocalproject.com.au",
}

SOURCE_NAME_MAP = {
    "gov.uk": "GOV.UK",
    "oecd.org": "OECD",
    "europa.eu": "European Union",
    "un.org": "United Nations",
    "wdo.org": "World Design Organization",
    "designcouncil.org.uk": "Design Council",
    "rca.ac.uk": "Royal College of Art",
    "gsd.harvard.edu": "Harvard Graduate School of Design",
    "ifdesign.com": "iF Design",
    "red-dot.org": "Red Dot",
    "bigsee.eu": "BIG SEE",
    "dezeen.com": "Dezeen",
    "designboom.com": "Designboom",
    "core77.com": "Core77",
    "itsnicethat.com": "It's Nice That",
    "creativeboom.com": "Creative Boom",
    "designweek.co.uk": "Design Week",
    "creativebloq.com": "Creative Bloq",
    "adage.com": "Ad Age",
    "retailgazette.co.uk": "Retail Gazette",
    "roboticsandautomationnews.com": "Robotics & Automation News",
    "tandfonline.com": "Taylor & Francis",
    "sciencedirect.com": "ScienceDirect",
    "springer.com": "Springer",
    "arxiv.org": "arXiv",
    "dl.acm.org": "ACM Digital Library",
}

DESIGN_SCHOOL_DOMAINS = (
    "rca.ac.uk",
    "gsd.harvard.edu",
    "mit.edu",
    "aalto.fi",
    "tudelft.nl",
    "polimi.it",
    "risd.edu",
    "artcenter.edu",
    "pratt.edu",
    "newschool.edu",
)

AWARD_SEARCH_DOMAINS = (
    "ifdesign.com",
    "red-dot.org",
    "bigsee.eu",
    "aia.org",
    "dezeen.com/awards",
    "core77.com/awards",
)

POLICY_SEARCH_DOMAINS = (
    "gov.uk",
    "oecd.org",
    "europa.eu",
    "un.org",
    "designcouncil.org.uk",
    "wdo.org",
)

JOURNAL_SEARCH_DOMAINS = (
    "tandfonline.com",
    "sciencedirect.com",
    "springer.com",
    "cambridge.org",
    "mitpressjournals.org",
    "dl.acm.org",
    "arxiv.org",
    "ijdesign.org",
    "designsociety.org",
)

VISUAL_SEARCH_DOMAINS = (
    "itsnicethat.com",
    "creativeboom.com",
    "designweek.co.uk",
    "creativebloq.com",
    "aiga.org",
    "cooperhewitt.org",
    "posterhouse.org",
    "moma.org",
)

QUERY_SPECS: tuple[QuerySpec, ...] = (
    QuerySpec(
        name="industrial-product",
        query="industrial design product design materials manufacturing mobility consumer electronics new launch official announcement",
        window_days=2,
        topic="news",
        category_hint="工业设计",
    ),
    QuerySpec(
        name="spatial-environment",
        query="architecture interior landscape spatial design public space exhibition new project research official",
        window_days=2,
        topic="news",
        category_hint="环境与空间设计",
    ),
    QuerySpec(
        name="service-social",
        query="service design public service prototyping civic design inclusive design social innovation official initiative",
        window_days=2,
        topic="news",
        category_hint="社会与服务设计",
    ),
    QuerySpec(
        name="business-strategy",
        query="design strategy design consulting organizational design customer experience brand innovation new practice",
        window_days=2,
        topic="news",
        category_hint="商业设计与战略",
    ),
    QuerySpec(
        name="innovation-methods",
        query="design methods systems design human centered design innovation practice new report design organization",
        window_days=2,
        topic="news",
        category_hint="创新设计",
    ),
    QuerySpec(
        name="design-schools",
        query="design school research lab new programme curriculum initiative product service architecture communication design",
        window_days=14,
        topic="general",
        category_hint="设计教育与研究",
        include_domains=DESIGN_SCHOOL_DOMAINS,
    ),
    QuerySpec(
        name="visual-communication",
        query="graphic design typography poster design visual identity branding motion design exhibition release",
        window_days=2,
        topic="news",
        category_hint="视觉设计前沿",
        include_domains=VISUAL_SEARCH_DOMAINS,
    ),
    QuerySpec(
        name="design-awards",
        query="2026 design award winner shortlist call for entries product architecture service graphic official",
        window_days=14,
        topic="general",
        category_hint="政策、奖项与机构",
        include_domains=AWARD_SEARCH_DOMAINS,
    ),
    QuerySpec(
        name="design-journals",
        query="newly published design research industrial design service design social design architecture design methods",
        window_days=14,
        topic="general",
        category_hint="设计教育与研究",
        include_domains=JOURNAL_SEARCH_DOMAINS,
    ),
    QuerySpec(
        name="design-policy",
        query="design policy public innovation service design prototyping government strategy official report",
        window_days=14,
        topic="general",
        category_hint="政策、奖项与机构",
        include_domains=POLICY_SEARCH_DOMAINS,
    ),
    QuerySpec(
        name="design-organizations",
        query="design council world design organization design standards public interest design new initiative official",
        window_days=14,
        topic="general",
        category_hint="政策、奖项与机构",
        include_domains=(
            "designcouncil.org.uk",
            "wdo.org",
            "aiga.org",
            "ico-d.org",
            "ixda.org",
            "service-design-network.org",
        ),
    ),
)

SOURCE_RUBRIC = """
价值评分总分100分：
- 来源权威性 25：A级原始来源优先；B级专业媒体须有明确日期与事实；C级来源不得入选。
- 战略影响 25：是否影响行业方向、方法、人才、商业模式、公共政策或供应链。
- 新颖性 15：是否为真正的新事件、新研究或新实践，而非长期页面或泛泛趋势文章。
- 设计专业相关性 15：必须直接涉及设计组织、方法、产品、空间、服务、视觉、教育、政策或战略。
- 团队可行动性 10：是否能转化为观察、研究、项目或内容选题。
- 证据质量 10：是否有明确主体、日期、原始链接和可核实事实。
低于72分不保留；若合格信息不足，宁可少选，也不得编造或降低事实标准。
""".strip()

SYSTEM_INSTRUCTION = """
你是一名严谨的全球设计行业研究编辑，为设计、咨询与创新团队制作每日内部早报。
只能依据用户提供的候选来源工作，不得补写候选材料中不存在的事实、日期、机构、人物或结论。

强制规则：
1. 每条正式新闻只能使用候选中的 source_url，并原样复制候选提供的 source_name_hint、source_type_hint、source_tier_hint、is_primary_source_hint 和 published_date。
2. C级来源不得入选。新闻稿转载、社交媒体和不明来源只能作为线索，不能作为最终证据。
3. 保留原文限定词：计划、目标、预计、拟议、待批准、最高可达、可能。不得把目标写成已实现，把拟议交易写成已完成。
4. 公司官方公告可以证明公司说了什么，但不等于独立媒体验证；表达应明确主体立场。
5. 不要因为文章中出现 design 一词就判定为设计行业新闻。生物分子设计、药物设计、法律中的产品缺陷、房产挂牌、招聘招生等通常不属于本早报。
6. visual_radar 的每条信号必须绑定一条候选来源，包含真实 source_url、published_date 和具体 evidence；证据不足时返回空数组。
7. 使用简体中文，表达专业、清晰、无营销腔。
""".strip()


def local_today() -> date:
    try:
        return datetime.now(ZoneInfo(REPORT_TIMEZONE)).date()
    except Exception:
        LOGGER.warning("Invalid REPORT_TIMEZONE=%s; falling back to UTC", REPORT_TIMEZONE)
        return datetime.now(timezone.utc).date()


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
        return urlsplit(url).netloc.lower().removeprefix("www.")
    except Exception:
        return "未知来源"


def domain_matches(domain: str, patterns: set[str] | tuple[str, ...]) -> bool:
    domain = domain.lower()
    for pattern in patterns:
        host = pattern.lower().split("/", 1)[0]
        if domain == host or domain.endswith("." + host):
            return True
    return False


def source_display_name(domain: str) -> str:
    for known, name in SOURCE_NAME_MAP.items():
        if domain_matches(domain, {known}):
            return name
    label = domain.split(".")[0].replace("-", " ").strip()
    return label.title() if label else domain


def is_government_or_international(domain: str) -> bool:
    return (
        domain.endswith(".gov")
        or ".gov." in domain
        or domain.endswith(".gov.uk")
        or domain_matches(domain, {"europa.eu", "oecd.org", "un.org", "worldbank.org"})
    )


def is_education_domain(domain: str) -> bool:
    return (
        domain.endswith(".edu")
        or ".edu." in domain
        or domain.endswith(".ac.uk")
        or domain.endswith(".ac.jp")
        or domain.endswith(".edu.sg")
    )


def classify_source(url: str, title: str, content: str) -> dict[str, object]:
    domain = domain_name(url)
    joined = f"{title} {content}".lower()
    path = urlsplit(url).path.lower()

    if domain_matches(domain, SOCIAL_DOMAINS):
        return {
            "source_name_hint": source_display_name(domain),
            "source_type_hint": "社交媒体",
            "source_tier_hint": "C",
            "is_primary_source_hint": False,
        }

    press_markers = ("business wire", "globenewswire", "pr newswire", "accesswire", "company announcement")
    if domain_matches(domain, PRESS_RELEASE_DOMAINS) or any(marker in joined for marker in press_markers):
        if "business wire" in joined:
            name = "Business Wire（转载）"
        elif "globenewswire" in joined:
            name = "GlobeNewswire（转载）"
        elif "pr newswire" in joined:
            name = "PR Newswire（转载）"
        else:
            name = f"{source_display_name(domain)}（新闻稿转载）"
        return {
            "source_name_hint": name,
            "source_type_hint": "新闻稿转载",
            "source_tier_hint": "C",
            "is_primary_source_hint": False,
        }

    if is_government_or_international(domain):
        return {
            "source_name_hint": source_display_name(domain),
            "source_type_hint": "政府或国际组织",
            "source_tier_hint": "A",
            "is_primary_source_hint": True,
        }

    if is_education_domain(domain) or domain_matches(domain, DESIGN_SCHOOL_DOMAINS):
        return {
            "source_name_hint": source_display_name(domain),
            "source_type_hint": "学院或研究机构",
            "source_tier_hint": "A",
            "is_primary_source_hint": True,
        }

    if domain_matches(domain, AWARD_DOMAINS):
        return {
            "source_name_hint": source_display_name(domain),
            "source_type_hint": "奖项官方机构",
            "source_tier_hint": "A",
            "is_primary_source_hint": True,
        }

    if domain_matches(domain, OFFICIAL_ORG_DOMAINS):
        return {
            "source_name_hint": source_display_name(domain),
            "source_type_hint": "专业机构",
            "source_tier_hint": "A",
            "is_primary_source_hint": True,
        }

    if domain_matches(domain, ACADEMIC_DOMAINS):
        return {
            "source_name_hint": source_display_name(domain),
            "source_type_hint": "学术期刊或论文库",
            "source_tier_hint": "A",
            "is_primary_source_hint": True,
        }

    if domain_matches(domain, PROFESSIONAL_MEDIA_DOMAINS):
        return {
            "source_name_hint": source_display_name(domain),
            "source_type_hint": "专业媒体",
            "source_tier_hint": "B",
            "is_primary_source_hint": False,
        }

    newsroom_markers = ("/newsroom", "/press", "/media", "/news/", "/releases", "/investors/news")
    if any(marker in path for marker in newsroom_markers):
        return {
            "source_name_hint": source_display_name(domain),
            "source_type_hint": "公司官方公告",
            "source_tier_hint": "A",
            "is_primary_source_hint": True,
        }

    return {
        "source_name_hint": source_display_name(domain),
        "source_type_hint": "其他来源",
        "source_tier_hint": "C",
        "is_primary_source_hint": False,
    }


def normalize_published_date(value: object) -> str:
    raw = compact_text(value, 120)
    if not raw or raw.lower() in {"unknown", "none", "null", "未知"}:
        return "未知"

    iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", raw)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(0)).isoformat()
        except ValueError:
            pass

    try:
        parsed = parsedate_to_datetime(raw)
        return parsed.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        pass

    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return "未知"


def extract_published_date_from_html(url: str) -> str:
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; DesignMorningBrief/1.0; "
                    "+https://github.com/)"
                )
            },
            timeout=12,
        )
        response.raise_for_status()
        html = response.text[:1_500_000]
    except requests.RequestException:
        return "未知"

    patterns = (
        r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)',
        r'name=["\'](?:date|pubdate|publish-date|publication_date|parsely-pub-date)["\'][^>]*content=["\']([^"\']+)',
        r'content=["\']([^"\']+)["\'][^>]*(?:property=["\']article:published_time["\']|name=["\'](?:date|pubdate|publish-date)["\'])',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'<time[^>]+datetime=["\']([^"\']+)',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            normalized = normalize_published_date(match.group(1))
            if normalized != "未知":
                return normalized
    return "未知"


def within_window(published_date: str, window_days: int, today: date) -> bool:
    if published_date == "未知":
        return False
    try:
        published = date.fromisoformat(published_date)
    except ValueError:
        return False
    age = (today - published).days
    return -1 <= age <= window_days


NEGATIVE_RELEVANCE_PHRASES = (
    "protein design",
    "drug design",
    "molecular design",
    "nuclease",
    "crispr",
    "gene editing",
    "products liability",
    "product liability",
    "manufacturing defects",
    "legal case",
    "lawsuit",
    "real estate listing",
    "asking rent",
    "property for sale",
    "career that machines",
    "bachelor's degree",
    "classes begin",
    "job opening",
    "hiring",
)

POSITIVE_DESIGN_PHRASES = (
    "industrial design",
    "product design",
    "service design",
    "social design",
    "civic design",
    "inclusive design",
    "design strategy",
    "design consulting",
    "organizational design",
    "experience design",
    "human-centered design",
    "human centred design",
    "systems design",
    "spatial design",
    "interior design",
    "landscape architecture",
    "urban design",
    "public space",
    "graphic design",
    "poster design",
    "typography",
    "visual identity",
    "branding",
    "motion design",
    "design award",
    "design council",
    "design school",
    "design research",
    "design methods",
    "design policy",
    "prototyping",
    "architecture",
    "design studio",
    "design practice",
)


def is_design_relevant(item: dict) -> bool:
    text = f"{item.get('title', '')} {item.get('content', '')}".lower()
    if any(phrase in text for phrase in NEGATIVE_RELEVANCE_PHRASES):
        return False
    if any(phrase in text for phrase in POSITIVE_DESIGN_PHRASES):
        return True

    category = item.get("category_hint")
    domain = str(item.get("source_domain", ""))
    if category in {"政策、奖项与机构", "设计教育与研究"} and item.get("source_tier_hint") == "A":
        return True
    if category == "工业设计" and any(
        term in text for term in ("product development", "materials innovation", "human machine interaction", "cobot")
    ):
        return True
    if category == "商业设计与战略" and any(
        term in text for term in ("brand strategy", "customer experience", "design capability", "creative strategy")
    ):
        return True
    if domain_matches(domain, OFFICIAL_ORG_DOMAINS | ACADEMIC_DOMAINS):
        return True
    return False


def candidate_priority(item: dict) -> float:
    tier_points = {"A": 40.0, "B": 24.0, "C": 0.0}
    score = tier_points.get(str(item.get("source_tier_hint")), 0.0)
    if item.get("is_primary_source_hint"):
        score += 12.0
    if item.get("published_date") != "未知":
        score += 15.0
    score += min(float(item.get("search_score") or 0.0), 1.0) * 20.0
    score += min(len(str(item.get("content", ""))) / 1000.0, 1.0) * 8.0
    return score


def tavily_search(
    client: TavilyClient,
    spec: QuerySpec,
    today: date,
    errors: list[str],
) -> tuple[list[dict], int]:
    start_date = (today - timedelta(days=spec.window_days)).isoformat()
    end_date = (today + timedelta(days=1)).isoformat()
    LOGGER.info("Tavily search: %s (%s, window=%sd)", spec.name, spec.query[:65], spec.window_days)

    kwargs: dict[str, object] = {
        "query": spec.query,
        "search_depth": "basic",
        "topic": spec.topic,
        "max_results": 6,
        "start_date": start_date,
        "end_date": end_date,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "include_favicon": False,
        "include_usage": True,
        "exclude_domains": sorted(SOCIAL_DOMAINS | LOW_QUALITY_DOMAINS),
    }
    if spec.include_domains:
        kwargs["include_domains"] = list(spec.include_domains)

    try:
        response = client.search(**kwargs)
    except Exception as exc:
        message = f"{spec.name}: {exc}"
        LOGGER.warning("Search failed: %s", message)
        errors.append(message)
        return [], 0

    usage = response.get("usage") or {}
    credits = int(usage.get("credits") or 1)
    results: list[dict] = []
    for result in response.get("results", []):
        if not result.get("url") or not result.get("title"):
            continue
        normalized_url = normalize_url(str(result["url"]))
        domain = domain_name(normalized_url)
        if domain_matches(domain, SOCIAL_DOMAINS | LOW_QUALITY_DOMAINS):
            continue
        item = {
            "query_name": spec.name,
            "query": spec.query,
            "category_hint": spec.category_hint,
            "window_days": spec.window_days,
            "title": compact_text(result.get("title"), 240),
            "url": normalized_url,
            "content": compact_text(result.get("content"), 1600),
            "search_score": result.get("score"),
            "published_date": normalize_published_date(
                result.get("published_date") or result.get("publishedDate")
            ),
            "source_domain": domain,
        }
        item.update(classify_source(normalized_url, item["title"], item["content"]))
        results.append(item)
    return results, credits


def primary_source_lookup(
    client: TavilyClient,
    lead: dict,
    today: date,
    errors: list[str],
) -> tuple[list[dict], int]:
    query = f'"{compact_text(lead.get("title"), 180)}" official press release'
    spec = QuerySpec(
        name=f"primary-source:{lead.get('query_name', 'lead')}",
        query=query,
        window_days=int(lead.get("window_days") or 14),
        topic="general",
        category_hint=lead.get("category_hint") or "创新设计",
    )
    results, credits = tavily_search(client, spec, today, errors)
    promoted = [
        item
        for item in results
        if item.get("source_tier_hint") in {"A", "B"}
        and item.get("source_type_hint") != "新闻稿转载"
    ]
    return promoted, credits


def run_searches(api_key: str) -> tuple[list[dict], list[str], int, list[dict]]:
    client = TavilyClient(api_key=api_key)
    today = local_today()
    errors: list[str] = []
    all_results: list[dict] = []
    credits_used = 0

    for spec in QUERY_SPECS:
        results, credits = tavily_search(client, spec, today, errors)
        all_results.extend(results)
        credits_used += credits

    press_leads = sorted(
        [
            item
            for item in all_results
            if item.get("source_type_hint") == "新闻稿转载"
            and is_design_relevant(item)
        ],
        key=lambda item: float(item.get("search_score") or 0.0),
        reverse=True,
    )[:MAX_PRIMARY_SOURCE_LOOKUPS]

    for lead in press_leads:
        promoted, credits = primary_source_lookup(client, lead, today, errors)
        all_results.extend(promoted)
        credits_used += credits

    deduped: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for item in sorted(all_results, key=candidate_priority, reverse=True):
        normalized = normalize_url(str(item["url"]))
        title_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(item["title"]).lower())[:140]
        if normalized in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(normalized)
        seen_titles.add(title_key)
        item["url"] = normalized
        deduped.append(item)

    # Resolve missing dates only for potentially usable A/B sources.
    for item in deduped[:30]:
        if item.get("published_date") == "未知" and item.get("source_tier_hint") in {"A", "B"}:
            resolved = extract_published_date_from_html(str(item["url"]))
            if resolved != "未知":
                item["published_date"] = resolved
                item["date_source"] = "page_metadata"

    accepted: list[dict] = []
    rejected: list[dict] = []
    for item in deduped:
        reason = ""
        if item.get("source_tier_hint") == "C":
            reason = "C级来源，仅作线索"
        elif not is_design_relevant(item):
            reason = "与专业设计行业相关性不足"
        elif item.get("published_date") == "未知":
            reason = "无法确认发布日期"
        elif not within_window(
            str(item.get("published_date")),
            int(item.get("window_days") or 2),
            today,
        ):
            reason = "发布时间超出对应窗口"
        if reason:
            rejected.append({**item, "rejection_reason": reason})
        else:
            accepted.append(item)

    accepted.sort(key=candidate_priority, reverse=True)
    return accepted[:MAX_CANDIDATES], errors, credits_used, rejected


def build_analysis_prompt(candidates: list[dict], search_errors: list[str], credits_used: int) -> str:
    report_date = local_today().isoformat()
    candidate_text = json.dumps(candidates, ensure_ascii=False, indent=2)
    return f"""
请基于下列已通过程序预筛选的候选来源，制作 {report_date} 的全球设计行业早报。

覆盖范围：工业设计、环境与空间设计、创新设计、社会与服务设计、商业设计与战略、视觉设计前沿、设计教育与研究、政策/奖项/机构。
常规新闻窗口为最近48小时；期刊、奖项、政策、展览、学校动态及重大机构报告窗口为最近14天。
候选中的日期、来源等级和来源类型已经由程序校验。不得自行修改。

筛选规则：
{SOURCE_RUBRIC}

输出要求：
1. 最终保留不超过 {MAX_ITEMS} 条，按 score 从高到低排序。
2. 每条必须复制候选材料中的真实 url、published_date、source_name_hint、source_type_hint、source_tier_hint 和 is_primary_source_hint。
3. 同一事件只留一条，优先原始、官方和A级来源。
4. summary 只写候选中明确出现的事实；保留“计划、目标、预计、拟议、待批准、最高可达”等限定词。
5. why_it_matters 写行业意义，但不得把单一案例夸大成全行业定论；team_action 写团队下一步。
6. 公司官方公告只能表述为“公司宣布/公司表示/双方计划”，不得写成已被第三方验证。
7. headline 与 executive_summary 归纳跨条目的共同信号，不得凭空添加事实。
8. visual_radar 最多5条。每条必须使用候选中的真实 source_url，并写明具体 evidence、source_name 和 published_date；没有足够证据就返回空数组。
9. methodology_note 不得声称“绝无错误”“完全客观”或“无任何虚构”。

运行数据：候选数量 {len(candidates)}；Tavily 约消耗 {credits_used} credits；筛选阈值 {MIN_SCORE}；搜索失败数 {len(search_errors)}。

候选来源 JSON：
{candidate_text}
""".strip()


def post_validate_brief(
    brief: MorningBrief,
    candidates: list[dict],
    credits_used: int,
    search_errors: list[str],
) -> MorningBrief:
    candidate_map = {normalize_url(str(item["url"])): item for item in candidates}
    validated_items: list[BriefItem] = []
    seen_urls: set[str] = set()

    for item in brief.items:
        url = normalize_url(str(item.source_url))
        candidate = candidate_map.get(url)
        if not candidate or url in seen_urls:
            continue
        if candidate.get("source_tier_hint") not in {"A", "B"}:
            continue
        if candidate.get("published_date") == "未知":
            continue

        score = item.score
        if candidate.get("source_tier_hint") == "B":
            score = min(score, 86)
        if not candidate.get("is_primary_source_hint"):
            score = min(score, 84)
        if score < MIN_SCORE:
            continue

        confidence = item.confidence
        if candidate.get("source_tier_hint") == "B" and confidence == "高":
            confidence = "中"

        data = item.model_dump()
        data.update(
            {
                "source_name": candidate["source_name_hint"],
                "source_type": candidate["source_type_hint"],
                "source_tier": candidate["source_tier_hint"],
                "is_primary_source": bool(candidate["is_primary_source_hint"]),
                "source_url": candidate["url"],
                "published_date": candidate["published_date"],
                "score": score,
                "confidence": confidence,
            }
        )
        validated_items.append(BriefItem.model_validate(data))
        seen_urls.add(url)

    validated_items.sort(key=lambda item: item.score, reverse=True)
    brief.items = validated_items[:MAX_ITEMS]
    if not brief.items:
        raise RuntimeError(f"没有达到 {MIN_SCORE} 分且通过来源/日期校验的新闻")

    validated_radar: list[VisualSignal] = []
    for signal in brief.visual_radar:
        url = normalize_url(str(signal.source_url))
        candidate = candidate_map.get(url)
        if not candidate:
            continue
        if candidate.get("source_tier_hint") not in {"A", "B"}:
            continue
        if candidate.get("published_date") == "未知":
            continue
        data = signal.model_dump()
        data.update(
            {
                "source_name": candidate["source_name_hint"],
                "source_url": candidate["url"],
                "published_date": candidate["published_date"],
            }
        )
        validated_radar.append(VisualSignal.model_validate(data))
    brief.visual_radar = validated_radar[:5]

    brief.report_date = local_today().isoformat()
    brief.methodology_note = (
        f"本报告由模型根据 {len(candidates)} 条公开候选来源自动生成，"
        f"Tavily 约消耗 {credits_used} credits，入选阈值 {MIN_SCORE} 分，"
        f"搜索失败 {len(search_errors)} 项。系统已执行来源、日期与重复性检查，"
        "但摘要和影响判断仍可能存在误差，重要决策请回到原始来源复核。"
    )
    return brief


def analyze_with_gemini(
    api_key: str,
    candidates: list[dict],
    search_errors: list[str],
    credits_used: int,
) -> MorningBrief:
    if len(candidates) < 5:
        raise RuntimeError(f"通过来源、日期和相关性校验的候选不足：仅 {len(candidates)} 条")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=DEFAULT_MODEL,
        contents=build_analysis_prompt(candidates, search_errors, credits_used),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=MorningBrief,
            max_output_tokens=12_000,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini 返回空内容")

    try:
        brief = MorningBrief.model_validate_json(response.text)
    except ValidationError as exc:
        raise RuntimeError(f"Gemini JSON 校验失败: {exc}") from exc

    return post_validate_brief(brief, candidates, credits_used, search_errors)


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
            f"**质量门槛**\n保留 **{len(brief.items)}** 条信息；"
            f"最低分 {min(i.score for i in brief.items)}；正式发送至少需要 {MIN_SEND_ITEMS} 条。"
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
            primary_label = "原始来源" if item.is_primary_source else "二手来源"
            source_line = (
                f"{compact_text(item.source_name, 42)} · {item.source_type} · "
                f"{item.source_tier}级/{primary_label} · {item.published_date} · 可信度{item.confidence}"
            )
            content = (
                f"**{offset}. [{item.category}] {compact_text(item.title_cn, 86)}**  `{item.score}/100`\n"
                f"{compact_text(item.summary, 230)}\n"
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

    if brief.visual_radar:
        radar_parts: list[str] = []
        for signal in brief.visual_radar:
            link = safe_link(str(signal.source_url))
            radar_parts.append(
                f"- **{compact_text(signal.signal, 120)}**\n"
                f"  证据：{compact_text(signal.evidence, 150)}\n"
                f"  {compact_text(signal.source_name, 45)} · {signal.published_date} · "
                f"<a href='{link}'>来源</a>"
            )
        radar_text = "\n".join(radar_parts)
    else:
        radar_text = "- 本期没有足够的可核验证据，暂不形成视觉趋势判断。"

    follow_lines = "\n".join(f"- {compact_text(x, 150)}" for x in brief.follow_ups[:4]) or "- 暂无。"
    end_elements = [
        markdown_element(f"**视觉与海报趋势雷达**\n{radar_text}"),
        markdown_element(f"**建议继续追踪**\n{follow_lines}"),
        markdown_element(f"**方法与局限**\n{compact_text(brief.methodology_note, 420)}"),
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
            raise RuntimeError(
                f"第 {index} 张卡片请求体 {size} bytes，超过安全上限 {FEISHU_SAFE_REQUEST_BYTES}"
            )
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


def save_outputs(
    brief: MorningBrief,
    candidates: list[dict],
    cards: list[dict],
    search_errors: list[str],
    rejected_candidates: list[dict],
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    (OUTPUT_DIR / "candidates.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUTPUT_DIR / "rejected-candidates.json").write_text(
        json.dumps(rejected_candidates, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUTPUT_DIR / "search-errors.json").write_text(
        json.dumps(search_errors, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for index, card in enumerate(cards, start=1):
        payload = {"msg_type": "interactive", "card": card}
        (OUTPUT_DIR / f"card-{index}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def demo_brief() -> MorningBrief:
    today = local_today().isoformat()
    return MorningBrief(
        report_date=today,
        headline="这是连通性测试卡片，不包含真实新闻。",
        executive_summary=(
            "此消息用于验证 GitHub Actions、签名校验与飞书 Webhook 是否连接成功。"
            "真实早报会在完整运行模式下调用 Tavily 与 Gemini。"
        ),
        items=[
            BriefItem(
                title_cn="飞书消息卡片连接测试",
                title_original="Feishu card connectivity test",
                category="政策、奖项与机构",
                source_name="Feishu Open Platform",
                source_type="专业机构",
                source_tier="A",
                is_primary_source=True,
                source_url="https://open.feishu.cn/",
                published_date=today,
                summary="程序已生成符合飞书卡片结构的测试内容，并准备通过自定义机器人 Webhook 发送。",
                why_it_matters="这证明云端运行环境能够读取仓库 Secrets 并与飞书机器人通信。",
                team_action="确认群聊收到卡片后，再运行完整的新闻搜索与分析流程。",
                score=100,
                confidence="高",
            )
        ],
        visual_radar=[],
        follow_ups=["检查卡片是否完整显示。", "确认链接能正常打开。"],
        methodology_note="测试模式：未调用 Tavily 与 Gemini，不消耗搜索或模型额度。",
    )


def run_full(send: bool = True) -> None:
    secrets = require_env(["TAVILY_API_KEY", "GEMINI_API_KEY"])
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    webhook_secret = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip() or None
    if send and not webhook_url:
        raise RuntimeError("发送模式缺少 FEISHU_WEBHOOK_URL")

    candidates, search_errors, credits_used, rejected_candidates = run_searches(
        secrets["TAVILY_API_KEY"]
    )
    LOGGER.info(
        "Accepted candidates: %s; rejected: %s; Tavily credits: %s",
        len(candidates),
        len(rejected_candidates),
        credits_used,
    )
    brief = analyze_with_gemini(
        secrets["GEMINI_API_KEY"],
        candidates,
        search_errors,
        credits_used,
    )
    cards = build_feishu_cards(brief)
    save_outputs(brief, candidates, cards, search_errors, rejected_candidates)

    if send and len(brief.items) < MIN_SEND_ITEMS:
        raise RuntimeError(
            f"仅 {len(brief.items)} 条新闻通过质量门槛，少于正式发送要求 {MIN_SEND_ITEMS} 条；"
            "结果已保存到 Artifact，但不会发送飞书"
        )

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
    save_outputs(brief, [], cards, [], [])
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
