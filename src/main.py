from __future__ import annotations

# VERIFIED BUILD: accepts 1+ candidates; no hard minimum of 5.

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
from difflib import SequenceMatcher
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


LOGGER = logging.getLogger("design-intelligence-brief")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
HISTORY_FILE = Path(os.getenv("HISTORY_FILE", "data/history.json"))
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
REPORT_TIMEZONE = os.getenv("REPORT_TIMEZONE", "Asia/Singapore")
MIN_SCORE = int(os.getenv("MIN_SCORE", "72"))
TARGET_ITEMS = int(os.getenv("TARGET_ITEMS", "6"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "8"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "60"))
MAX_DETAIL_LOOKUPS = int(os.getenv("MAX_DETAIL_LOOKUPS", "4"))
MAX_PRIMARY_SOURCE_LOOKUPS = int(os.getenv("MAX_PRIMARY_SOURCE_LOOKUPS", "3"))
HISTORY_FRESH_DAYS = int(os.getenv("HISTORY_FRESH_DAYS", "45"))
HISTORY_CLASSIC_DAYS = int(os.getenv("HISTORY_CLASSIC_DAYS", "180"))
FEISHU_SAFE_REQUEST_BYTES = 18_500
CODE_VERSION = "2026-07-layered-v3-verified"

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
ContentType = Literal["今日新讯", "近期洞察", "经典方法"]
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
    title_original: str = Field(description="来源原始标题")
    content_type: ContentType
    time_horizon: str = Field(description="48小时、14天、近6个月、近5年或经典基础")
    category: Category
    source_name: str
    source_type: SourceType = "其他来源"
    source_tier: SourceTier = "C"
    is_primary_source: bool = False
    source_url: HttpUrl
    published_date: str = Field(description="YYYY-MM-DD")
    summary: str = Field(description="核心事实；保留计划、预计、拟议、最高可达等限定词")
    why_it_matters: str = Field(description="行业意义")
    why_now: str = Field(description="为什么此刻值得团队关注；旧资料必须解释当前适用性")
    team_action: str = Field(description="团队可执行的观察或动作")
    score: int = Field(ge=0, le=100)
    confidence: Literal["高", "中", "低"]

    @field_validator(
        "title_cn",
        "title_original",
        "time_horizon",
        "source_name",
        "published_date",
        "summary",
        "why_it_matters",
        "why_now",
        "team_action",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class VisualSignal(BaseModel):
    signal: str
    evidence: str
    source_name: str
    source_url: HttpUrl
    published_date: str
    confidence: Literal["高", "中", "低"]

    @field_validator("signal", "evidence", "source_name", "published_date")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class MorningBrief(BaseModel):
    report_date: str
    headline: str
    executive_summary: str
    items: list[BriefItem]
    visual_radar: list[VisualSignal] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    methodology_note: str


@dataclass(frozen=True)
class QuerySpec:
    name: str
    query: str
    stage: Literal["fresh", "recent", "classic", "foundational"]
    window_days: int
    topic: Literal["general", "news"]
    category_hint: Category
    include_domains: tuple[str, ...] = ()

    @property
    def content_type(self) -> ContentType:
        if self.stage == "fresh":
            return "今日新讯"
        if self.stage == "recent":
            return "近期洞察"
        return "经典方法"

    @property
    def time_horizon(self) -> str:
        if self.stage == "fresh" and self.window_days <= 2:
            return "48小时"
        if self.stage == "fresh":
            return "14天"
        if self.stage == "recent":
            return "近6个月"
        if self.stage == "classic":
            return "近5年"
        return "经典基础"


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
LOW_QUALITY_DOMAINS = {"medium.com", "substack.com", "reddit.com", "quora.com"}
PRESS_RELEASE_DOMAINS = {
    "businesswire.com",
    "globenewswire.com",
    "prnewswire.com",
    "accesswire.com",
    "einpresswire.com",
    "markets.ft.com",
}
AWARD_DOMAINS = {"ifdesign.com", "red-dot.org", "bigsee.eu", "aia.org"}
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
    "jstor.org",
    "sagepub.com",
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
    "consultancy.asia",
    "consultancy.org",
    "automotiveworld.com",
    "roboticsupdate.com",
}
DESIGN_SCHOOL_DOMAINS = {
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
}
SOURCE_NAME_MAP = {
    "gov.uk": "GOV.UK",
    "oecd.org": "OECD",
    "europa.eu": "European Union",
    "un.org": "United Nations",
    "worldbank.org": "World Bank",
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
    "roboticsupdate.com": "Robotics Update",
    "consultancy.asia": "Consultancy.asia",
    "consultancy.org": "Consultancy.org",
    "automotiveworld.com": "Automotive World",
    "tandfonline.com": "Taylor & Francis",
    "sciencedirect.com": "ScienceDirect",
    "springer.com": "Springer",
    "arxiv.org": "arXiv",
    "dl.acm.org": "ACM Digital Library",
    "jstor.org": "JSTOR",
}

SEARCH_DOMAINS = tuple(
    sorted(
        PROFESSIONAL_MEDIA_DOMAINS
        | OFFICIAL_ORG_DOMAINS
        | ACADEMIC_DOMAINS
        | DESIGN_SCHOOL_DOMAINS
        | AWARD_DOMAINS
        | {"gov.uk", "oecd.org", "europa.eu", "un.org", "worldbank.org"}
    )
)

FRESH_QUERIES: tuple[QuerySpec, ...] = (
    QuerySpec(
        "industrial-product",
        "industrial design product design materials manufacturing mobility consumer electronics official announcement",
        "fresh",
        2,
        "news",
        "工业设计",
    ),
    QuerySpec(
        "spatial-environment",
        "architecture interior landscape spatial design public space exhibition new project research official",
        "fresh",
        2,
        "news",
        "环境与空间设计",
    ),
    QuerySpec(
        "service-social",
        "service design public service prototyping civic inclusive social innovation official initiative",
        "fresh",
        2,
        "news",
        "社会与服务设计",
    ),
    QuerySpec(
        "business-strategy",
        "design strategy design consulting organizational design customer experience brand innovation new practice",
        "fresh",
        2,
        "news",
        "商业设计与战略",
    ),
    QuerySpec(
        "visual-communication",
        "graphic design typography poster visual identity branding motion design new release",
        "fresh",
        2,
        "news",
        "视觉设计前沿",
        tuple(sorted(PROFESSIONAL_MEDIA_DOMAINS | {"aiga.org", "posterhouse.org", "cooperhewitt.org"})),
    ),
    QuerySpec(
        "awards-policy",
        "design award winner shortlist design policy public innovation official report",
        "fresh",
        14,
        "general",
        "政策、奖项与机构",
        tuple(sorted(AWARD_DOMAINS | OFFICIAL_ORG_DOMAINS | {"gov.uk", "oecd.org", "europa.eu"})),
    ),
    QuerySpec(
        "schools-labs",
        "design school research lab new programme curriculum initiative industrial service communication design",
        "fresh",
        14,
        "general",
        "设计教育与研究",
        tuple(sorted(DESIGN_SCHOOL_DOMAINS)),
    ),
    QuerySpec(
        "journals-reports",
        "newly published design research service design industrial design social design methods report",
        "fresh",
        14,
        "general",
        "设计教育与研究",
        tuple(sorted(ACADEMIC_DOMAINS | OFFICIAL_ORG_DOMAINS)),
    ),
)

RECENT_QUERIES: tuple[QuerySpec, ...] = (
    QuerySpec(
        "recent-design-methods",
        "design methods case study systems design human centered design implementation report",
        "recent",
        180,
        "general",
        "创新设计",
        SEARCH_DOMAINS,
    ),
    QuerySpec(
        "recent-service-design",
        "service design public service case study prototyping evaluation implementation",
        "recent",
        180,
        "general",
        "社会与服务设计",
        SEARCH_DOMAINS,
    ),
    QuerySpec(
        "recent-business-design",
        "business design design strategy consulting organizational transformation customer experience case study",
        "recent",
        180,
        "general",
        "商业设计与战略",
        SEARCH_DOMAINS,
    ),
    QuerySpec(
        "recent-spatial",
        "spatial design architecture interior public space design research case study",
        "recent",
        180,
        "general",
        "环境与空间设计",
        SEARCH_DOMAINS,
    ),
    QuerySpec(
        "recent-industrial",
        "industrial design product development materials circular design case study research",
        "recent",
        180,
        "general",
        "工业设计",
        SEARCH_DOMAINS,
    ),
    QuerySpec(
        "recent-visual",
        "visual identity graphic design typography branding case study design process",
        "recent",
        180,
        "general",
        "视觉设计前沿",
        SEARCH_DOMAINS,
    ),
)

CLASSIC_QUERIES: tuple[QuerySpec, ...] = (
    QuerySpec(
        "classic-service-design",
        "service design methods framework evaluation co-design peer reviewed",
        "classic",
        1825,
        "general",
        "社会与服务设计",
        tuple(sorted(ACADEMIC_DOMAINS | OFFICIAL_ORG_DOMAINS)),
    ),
    QuerySpec(
        "classic-systems-design",
        "systems design methods framework design research peer reviewed",
        "classic",
        1825,
        "general",
        "创新设计",
        tuple(sorted(ACADEMIC_DOMAINS | OFFICIAL_ORG_DOMAINS)),
    ),
    QuerySpec(
        "classic-participatory-design",
        "participatory design social design co-design framework research",
        "classic",
        1825,
        "general",
        "社会与服务设计",
        tuple(sorted(ACADEMIC_DOMAINS | OFFICIAL_ORG_DOMAINS)),
    ),
    QuerySpec(
        "classic-design-strategy",
        "design strategy framework organizational design business design research",
        "classic",
        1825,
        "general",
        "商业设计与战略",
        tuple(sorted(ACADEMIC_DOMAINS | OFFICIAL_ORG_DOMAINS | DESIGN_SCHOOL_DOMAINS)),
    ),
    QuerySpec(
        "classic-product-spatial",
        "industrial design spatial design methodology circular inclusive design research",
        "classic",
        1825,
        "general",
        "工业设计",
        tuple(sorted(ACADEMIC_DOMAINS | OFFICIAL_ORG_DOMAINS | DESIGN_SCHOOL_DOMAINS)),
    ),
)

FOUNDATIONAL_QUERIES: tuple[QuerySpec, ...] = (
    QuerySpec(
        "foundational-human-centered",
        "seminal human centered design framework design methods research",
        "foundational",
        7300,
        "general",
        "创新设计",
        tuple(sorted(ACADEMIC_DOMAINS | OFFICIAL_ORG_DOMAINS | DESIGN_SCHOOL_DOMAINS)),
    ),
    QuerySpec(
        "foundational-service-systems",
        "seminal service design systems design participatory design framework",
        "foundational",
        7300,
        "general",
        "社会与服务设计",
        tuple(sorted(ACADEMIC_DOMAINS | OFFICIAL_ORG_DOMAINS | DESIGN_SCHOOL_DOMAINS)),
    ),
)

SCORE_RUBRIC = """
今日新讯（100分）：来源权威25、行业影响25、时效性15、设计相关性15、可行动性10、证据质量10。
近期洞察（100分）：来源权威25、案例/研究深度20、当前适用性20、设计相关性15、可迁移性10、证据质量10。
经典方法（100分）：来源与研究质量30、长期有效性25、可迁移性20、框架清晰度15、当下相关性10。
低于72分不保留。不要用旧资料冒充今日新闻，也不要为了数量降低标准。
""".strip()

SYSTEM_INSTRUCTION = """
你是一名严谨的全球设计情报编辑，为设计、咨询与创新团队制作每日内部早报。
只能依据用户提供的候选来源工作，不得补写候选材料中不存在的事实、日期、机构、人物、指标或结论。

强制规则：
1. 每条正式条目必须复制候选中的 source_url、source_name_hint、source_type_hint、source_tier_hint、is_primary_source_hint、published_date、content_type_hint 和 time_horizon_hint。
2. C级来源不得入选。栏目页、标签页、搜索页和聚合首页不得作为最终证据。
3. 保留原文限定词：计划、目标、预计、拟议、待批准、最高可达、可能。不得把目标写成已实现，把拟议交易写成已完成。
4. 今日新讯必须是对应窗口内的新事件；近期洞察和经典方法必须在 why_now 中解释为什么现在仍值得关注。
5. 公司官方公告只证明公司发布了该说法，不等于独立第三方验证。
6. 不要因为文章出现 design 一词就判定为设计行业内容。生物分子设计、药物设计、法律产品缺陷、房地产挂牌、招生广告通常不属于本早报。
7. visual_radar 每条必须绑定候选中的真实详情页、日期和具体证据；证据不足时返回空数组。
8. 使用简体中文，表达专业、清晰、克制。若新讯较少，应明确说明，并使用近期洞察或经典方法补充，而不是伪造趋势。
""".strip()

NEGATIVE_RELEVANCE_PHRASES = (
    "protein design",
    "drug design",
    "molecular design",
    "designed enzyme",
    "design defect",
    "manufacturing defect",
    "product liability",
    "real estate listing",
    "asking rent",
    "job opening",
    "admissions open",
)
POSITIVE_DESIGN_TERMS = (
    "industrial design",
    "product design",
    "service design",
    "social design",
    "systems design",
    "design strategy",
    "design consulting",
    "organizational design",
    "human centered design",
    "human-centred design",
    "participatory design",
    "co-design",
    "spatial design",
    "interior design",
    "landscape architecture",
    "public space",
    "visual identity",
    "graphic design",
    "typography",
    "poster design",
    "design research",
    "design methods",
    "design award",
    "design policy",
    "interaction design",
    "inclusive design",
    "circular design",
)
LISTING_SEGMENTS = {
    "category",
    "categories",
    "tag",
    "tags",
    "topics",
    "search",
    "archive",
    "archives",
    "page",
}
LISTING_EXACT_PATHS = {
    "/",
    "/news",
    "/articles",
    "/features",
    "/projects",
    "/research",
    "/publications",
    "/graphic-design",
    "/media/typography",
    "/typography",
}


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
        if value:
            values[name] = value
        else:
            missing.append(name)
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")
    return values


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        path = re.sub(r"/$", "", parts.path) or "/"
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
    return any(domain == host or domain.endswith("." + host) for host in patterns)


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
        return source_hints(domain, "社交媒体", "C", False)
    if is_government_or_international(domain):
        return source_hints(domain, "政府或国际组织", "A", True)
    if is_education_domain(domain) or domain_matches(domain, DESIGN_SCHOOL_DOMAINS):
        return source_hints(domain, "学院或研究机构", "A", True)
    if domain_matches(domain, AWARD_DOMAINS):
        return source_hints(domain, "奖项官方机构", "A", True)
    if domain_matches(domain, OFFICIAL_ORG_DOMAINS):
        return source_hints(domain, "专业机构", "A", True)
    if domain_matches(domain, ACADEMIC_DOMAINS):
        return source_hints(domain, "学术期刊或论文库", "A", True)

    newsroom_markers = ("/newsroom", "/press-releases", "/media-center", "/releases", "/investors/news")
    if any(marker in path for marker in newsroom_markers) and not domain_matches(domain, PROFESSIONAL_MEDIA_DOMAINS):
        return source_hints(domain, "公司官方公告", "A", True)

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

    if domain_matches(domain, PROFESSIONAL_MEDIA_DOMAINS):
        return source_hints(domain, "专业媒体", "B", False)
    return source_hints(domain, "其他来源", "C", False)


def source_hints(domain: str, source_type: SourceType, tier: SourceTier, primary: bool) -> dict[str, object]:
    return {
        "source_name_hint": source_display_name(domain),
        "source_type_hint": source_type,
        "source_tier_hint": tier,
        "is_primary_source_hint": primary,
    }


def normalize_published_date(value: object) -> str:
    raw = compact_text(value, 180)
    if not raw or raw.lower() in {"unknown", "none", "null", "未知"}:
        return "未知"
    iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", raw)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(0)).isoformat()
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return "未知"


def extract_date_from_url_or_text(url: str, title: str, content: str) -> str:
    path = urlsplit(url).path
    patterns = (
        r"/(20\d{2})/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?:/|$)",
        r"\b(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])\b",
    )
    for pattern in patterns:
        match = re.search(pattern, path)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
            except ValueError:
                pass
    combined = f"{title} {content}"
    for pattern in (
        r"\b([A-Z][a-z]+ \d{1,2}, 20\d{2})\b",
        r"\b(\d{1,2} [A-Z][a-z]+ 20\d{2})\b",
        r"\b(20\d{2}-\d{2}-\d{2})\b",
    ):
        match = re.search(pattern, combined)
        if match:
            parsed = normalize_published_date(match.group(1))
            if parsed != "未知":
                return parsed
    return "未知"


def extract_published_date_from_html(url: str) -> str:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DesignIntelligenceBrief/2.0)"},
            timeout=12,
        )
        response.raise_for_status()
        html = response.text[:1_500_000]
    except requests.RequestException:
        return "未知"
    patterns = (
        r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)',
        r'name=["\'](?:date|pubdate|publish-date|publication_date|parsely-pub-date)["\'][^>]*content=["\']([^"\']+)',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'<time[^>]+datetime=["\']([^"\']+)',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            parsed = normalize_published_date(match.group(1))
            if parsed != "未知":
                return parsed
    return "未知"


def is_listing_page(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.lower().rstrip("/") or "/"
    if path in LISTING_EXACT_PATHS:
        return True
    segments = [segment for segment in path.split("/") if segment]
    if any(segment in LISTING_SEGMENTS for segment in segments):
        return True
    if any(key in dict(parse_qsl(parts.query)) for key in ("s", "search", "q", "page")):
        return True
    if domain_matches(domain_name(url), {"itsnicethat.com"}) and len(segments) <= 2:
        return True
    return False


def within_window(published_date: str, window_days: int, today: date) -> bool:
    try:
        published = date.fromisoformat(published_date)
    except ValueError:
        return False
    age = (today - published).days
    return -1 <= age <= window_days


def is_design_relevant(item: dict) -> bool:
    text = f"{item.get('title', '')} {item.get('content', '')}".lower()
    if any(phrase in text for phrase in NEGATIVE_RELEVANCE_PHRASES):
        return False
    matches = sum(1 for term in POSITIVE_DESIGN_TERMS if term in text)
    return matches >= 1 or item.get("source_tier_hint") == "A" and "design" in text


def candidate_priority(item: dict) -> float:
    tier_score = {"A": 3.0, "B": 2.0, "C": 0.0}.get(str(item.get("source_tier_hint")), 0.0)
    stage_score = {"fresh": 0.6, "recent": 0.35, "classic": 0.2, "foundational": 0.1}.get(
        str(item.get("stage")), 0.0
    )
    primary = 0.35 if item.get("is_primary_source_hint") else 0.0
    detail = 0.25 if not is_listing_page(str(item.get("url", ""))) else -0.5
    return tier_score + stage_score + primary + detail + float(item.get("search_score") or 0.0)


def title_similarity(a: str, b: str) -> float:
    normalized_a = re.sub(r"\W+", " ", a.lower()).strip()
    normalized_b = re.sub(r"\W+", " ", b.lower()).strip()
    return SequenceMatcher(None, normalized_a, normalized_b).ratio()


def tavily_search(client: TavilyClient, spec: QuerySpec, today: date, errors: list[str], max_results: int = 5) -> tuple[list[dict], int]:
    start_date = (today - timedelta(days=spec.window_days)).isoformat()
    end_date = (today + timedelta(days=1)).isoformat()
    LOGGER.info("Tavily search: %s (%s, window=%sd)", spec.name, spec.stage, spec.window_days)
    kwargs: dict[str, object] = {
        "query": spec.query,
        "search_depth": "basic",
        "topic": spec.topic,
        "max_results": max_results,
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

    credits = int((response.get("usage") or {}).get("credits") or 1)
    results: list[dict] = []
    for result in response.get("results", []):
        if not result.get("url") or not result.get("title"):
            continue
        url = normalize_url(str(result["url"]))
        domain = domain_name(url)
        if domain_matches(domain, SOCIAL_DOMAINS | LOW_QUALITY_DOMAINS):
            continue
        title = compact_text(result.get("title"), 260)
        content = compact_text(result.get("content"), 1800)
        published = normalize_published_date(result.get("published_date") or result.get("publishedDate"))
        if published == "未知":
            published = extract_date_from_url_or_text(url, title, content)
        item = {
            "query_name": spec.name,
            "query": spec.query,
            "stage": spec.stage,
            "content_type_hint": spec.content_type,
            "time_horizon_hint": spec.time_horizon,
            "category_hint": spec.category_hint,
            "window_days": spec.window_days,
            "title": title,
            "url": url,
            "content": content,
            "search_score": result.get("score"),
            "published_date": published,
            "source_domain": domain,
        }
        item.update(classify_source(url, title, content))
        results.append(item)
    return results, credits


def exact_title_lookup(client: TavilyClient, lead: dict, today: date, errors: list[str]) -> tuple[list[dict], int]:
    domain = domain_name(str(lead["url"]))
    spec = QuerySpec(
        name=f"detail:{lead.get('query_name', 'lead')}",
        query=f'"{compact_text(lead.get("title"), 180)}"',
        stage=str(lead.get("stage") or "fresh"),  # type: ignore[arg-type]
        window_days=int(lead.get("window_days") or 14),
        topic="general",
        category_hint=lead.get("category_hint") or "创新设计",
        include_domains=(domain,),
    )
    results, credits = tavily_search(client, spec, today, errors, max_results=3)
    matching = [
        item
        for item in results
        if not is_listing_page(str(item["url"]))
        and title_similarity(str(lead.get("title", "")), str(item.get("title", ""))) >= 0.45
    ]
    return matching, credits


def primary_source_lookup(client: TavilyClient, lead: dict, today: date, errors: list[str]) -> tuple[list[dict], int]:
    spec = QuerySpec(
        name=f"primary:{lead.get('query_name', 'lead')}",
        query=f'"{compact_text(lead.get("title"), 180)}" official',
        stage=str(lead.get("stage") or "fresh"),  # type: ignore[arg-type]
        window_days=int(lead.get("window_days") or 14),
        topic="general",
        category_hint=lead.get("category_hint") or "创新设计",
    )
    results, credits = tavily_search(client, spec, today, errors, max_results=4)
    return [item for item in results if item.get("source_tier_hint") == "A"], credits


def dedupe_candidates(items: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for item in sorted(items, key=candidate_priority, reverse=True):
        url = normalize_url(str(item["url"]))
        title_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(item["title"]).lower())[:150]
        if url in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title_key)
        item["url"] = url
        deduped.append(item)
    return deduped


def resolve_candidates(client: TavilyClient, items: list[dict], today: date, errors: list[str]) -> tuple[list[dict], int]:
    credits = 0
    resolved: list[dict] = []
    listing_count = 0
    primary_count = 0
    for item in dedupe_candidates(items):
        replacement: dict | None = None
        if is_listing_page(str(item["url"])) and listing_count < MAX_DETAIL_LOOKUPS:
            matches, used = exact_title_lookup(client, item, today, errors)
            credits += used
            listing_count += 1
            if matches:
                replacement = max(matches, key=candidate_priority)
        if replacement is None and item.get("source_tier_hint") == "C" and primary_count < MAX_PRIMARY_SOURCE_LOOKUPS:
            matches, used = primary_source_lookup(client, item, today, errors)
            credits += used
            primary_count += 1
            if matches:
                replacement = max(matches, key=candidate_priority)
        resolved.append(replacement or item)
    return dedupe_candidates(resolved), credits


def load_history() -> list[dict]:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def recently_used(url: str, content_type: str, history: list[dict], today: date) -> bool:
    limit = HISTORY_CLASSIC_DAYS if content_type == "经典方法" else HISTORY_FRESH_DAYS
    normalized = normalize_url(url)
    for entry in history:
        if normalize_url(str(entry.get("url", ""))) != normalized:
            continue
        try:
            selected = date.fromisoformat(str(entry.get("selected_date")))
        except ValueError:
            continue
        if 0 <= (today - selected).days <= limit:
            return True
    return False


def filter_candidates(items: list[dict], history: list[dict], today: date) -> tuple[list[dict], list[dict]]:
    accepted: list[dict] = []
    rejected: list[dict] = []
    for item in dedupe_candidates(items):
        reason = ""
        url = str(item["url"])
        if item.get("source_tier_hint") == "C":
            reason = "C级来源，仅作线索"
        elif is_listing_page(url):
            reason = "栏目页、标签页或聚合页"
        elif not is_design_relevant(item):
            reason = "与专业设计行业相关性不足"
        elif item.get("published_date") == "未知":
            resolved = extract_published_date_from_html(url)
            if resolved != "未知":
                item["published_date"] = resolved
                item["date_source"] = "page_metadata"
            else:
                reason = "无法确认发布日期"
        if not reason and not within_window(str(item.get("published_date")), int(item.get("window_days") or 2), today):
            reason = "发布时间超出对应时间层"
        if not reason and recently_used(url, str(item.get("content_type_hint")), history, today):
            reason = "近期早报已使用，避免重复"
        if reason:
            rejected.append({**item, "rejection_reason": reason})
        else:
            accepted.append(item)
    accepted.sort(key=candidate_priority, reverse=True)
    return accepted, rejected


def run_searches(api_key: str) -> tuple[list[dict], list[str], int, list[dict], dict]:
    client = TavilyClient(api_key=api_key)
    today = local_today()
    history = load_history()
    errors: list[str] = []
    accepted_all: list[dict] = []
    rejected_all: list[dict] = []
    credits_used = 0
    stage_summary: dict[str, dict[str, int]] = {}

    stages = (
        ("fresh", FRESH_QUERIES),
        ("recent", RECENT_QUERIES),
        ("classic", CLASSIC_QUERIES),
        ("foundational", FOUNDATIONAL_QUERIES),
    )
    for stage_name, specs in stages:
        if stage_name != "fresh" and len(accepted_all) >= TARGET_ITEMS:
            break
        raw: list[dict] = []
        stage_credits = 0
        for spec in specs:
            results, used = tavily_search(client, spec, today, errors)
            raw.extend(results)
            stage_credits += used
        resolved, extra = resolve_candidates(client, raw, today, errors)
        stage_credits += extra
        accepted, rejected = filter_candidates(resolved, history, today)
        existing_urls = {normalize_url(str(item["url"])) for item in accepted_all}
        accepted_all.extend(item for item in accepted if normalize_url(str(item["url"])) not in existing_urls)
        rejected_all.extend(rejected)
        credits_used += stage_credits
        stage_summary[stage_name] = {
            "searched": len(raw),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "credits": stage_credits,
        }
        LOGGER.info(
            "Stage %s: accepted=%s rejected=%s total=%s credits=%s",
            stage_name,
            len(accepted),
            len(rejected),
            len(accepted_all),
            stage_credits,
        )

    accepted_all = dedupe_candidates(accepted_all)[:MAX_CANDIDATES]
    return accepted_all, errors, credits_used, rejected_all, stage_summary


def build_analysis_prompt(candidates: list[dict], search_errors: list[str], credits_used: int, stage_summary: dict) -> str:
    report_date = local_today().isoformat()
    return f"""
请基于下列已通过程序验证的候选来源，制作 {report_date} 的全球设计情报与知识早报。

三层内容结构：
- 今日新讯：最近48小时；奖项、政策、学院、期刊、展览和重大机构信息可使用最近14天。
- 近期洞察：最近6个月内仍具项目参考价值的案例、报告、研究和组织实践。
- 经典方法：近5年内长期有效的方法、框架和研究；经典基础池可能更早，但必须解释当下价值。

评分规则：
{SCORE_RUBRIC}

输出要求：
1. 最终保留1-{MAX_ITEMS}条，按内容类型和score组织；不要为凑数降低标准。
2. 新讯不足时，用近期洞察和经典方法补充。旧资料不得写成今日发生的新闻。
3. 每条必须复制候选中的真实url、日期、来源元数据、content_type_hint和time_horizon_hint。
4. summary只写候选明确支持的事实；why_it_matters写设计行业意义；why_now解释为什么此刻值得阅读；team_action写团队动作。
5. 对经典方法，why_now必须说明它如何迁移到今天的项目、组织或决策。
6. 同一事件或同一方法只保留一次。优先原始、官方和A级来源。
7. visual_radar最多4条，每条必须使用候选中的真实详情页和具体证据；证据不足则返回空数组。
8. headline和executive_summary必须区分新讯与历史资料，不得把经典内容称作今天发生。
9. methodology_note保持审慎，不得声称绝对无误。

运行数据：候选{len(candidates)}条；Tavily约消耗{credits_used} credits；阈值{MIN_SCORE}；搜索失败{len(search_errors)}；分阶段数据{json.dumps(stage_summary, ensure_ascii=False)}。

候选来源JSON：
{json.dumps(candidates, ensure_ascii=False, indent=2)}
""".strip()


def post_validate_brief(brief: MorningBrief, candidates: list[dict], credits_used: int, search_errors: list[str], stage_summary: dict) -> MorningBrief:
    candidate_map = {normalize_url(str(item["url"])): item for item in candidates}
    validated: list[BriefItem] = []
    seen: set[str] = set()
    for item in brief.items:
        url = normalize_url(str(item.source_url))
        candidate = candidate_map.get(url)
        if not candidate or url in seen or is_listing_page(url):
            continue
        if candidate.get("source_tier_hint") not in {"A", "B"}:
            continue
        score = item.score
        if candidate.get("source_tier_hint") == "B":
            score = min(score, 86)
        if not candidate.get("is_primary_source_hint"):
            score = min(score, 84)
        if candidate.get("content_type_hint") == "经典方法" and candidate.get("source_tier_hint") == "B":
            score = min(score, 82)
        if score < MIN_SCORE:
            continue
        confidence = item.confidence
        if candidate.get("source_tier_hint") == "B" and confidence == "高":
            confidence = "中"
        data = item.model_dump()
        data.update(
            {
                "content_type": candidate["content_type_hint"],
                "time_horizon": candidate["time_horizon_hint"],
                "category": candidate["category_hint"],
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
        validated.append(BriefItem.model_validate(data))
        seen.add(url)

    order = {"今日新讯": 0, "近期洞察": 1, "经典方法": 2}
    validated.sort(key=lambda item: (order[item.content_type], -item.score))
    brief.items = validated[:MAX_ITEMS]

    radar: list[VisualSignal] = []
    for signal in brief.visual_radar:
        url = normalize_url(str(signal.source_url))
        candidate = candidate_map.get(url)
        if not candidate or candidate.get("source_tier_hint") not in {"A", "B"} or is_listing_page(url):
            continue
        data = signal.model_dump()
        data.update(
            {
                "source_name": candidate["source_name_hint"],
                "source_url": candidate["url"],
                "published_date": candidate["published_date"],
            }
        )
        radar.append(VisualSignal.model_validate(data))
    brief.visual_radar = radar[:4]
    brief.report_date = local_today().isoformat()
    counts = {kind: sum(1 for item in brief.items if item.content_type == kind) for kind in ("今日新讯", "近期洞察", "经典方法")}
    brief.methodology_note = (
        f"本报告从{len(candidates)}条已验证候选中生成：今日新讯{counts['今日新讯']}条、"
        f"近期洞察{counts['近期洞察']}条、经典方法{counts['经典方法']}条。"
        f"Tavily约消耗{credits_used} credits，入选阈值{MIN_SCORE}分，搜索失败{len(search_errors)}项。"
        "旧资料均明确标注时间层，摘要和影响判断仍需在重要决策前回到原始来源复核。"
    )
    return brief


def empty_brief(credits_used: int, search_errors: list[str]) -> MorningBrief:
    return MorningBrief(
        report_date=local_today().isoformat(),
        headline="今日未发现达到来源与相关性标准的设计情报",
        executive_summary="系统已依次检索最新动态、近期洞察与经典方法，但本次没有找到同时满足来源、日期、详情页和设计相关性要求的内容。为避免误导，本期不填充低质量条目。",
        items=[],
        visual_radar=[],
        follow_ups=["检查搜索源是否暂时不可访问。", "下一次运行继续使用分层回溯策略。"],
        methodology_note=f"本次Tavily约消耗{credits_used} credits，搜索失败{len(search_errors)}项；未用低质量内容补位。",
    )


def analyze_with_gemini(api_key: str, candidates: list[dict], search_errors: list[str], credits_used: int, stage_summary: dict) -> MorningBrief:
    if not candidates:
        return empty_brief(credits_used, search_errors)
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=DEFAULT_MODEL,
        contents=build_analysis_prompt(candidates, search_errors, credits_used, stage_summary),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=MorningBrief,
            max_output_tokens=14_000,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini返回空内容")
    try:
        brief = MorningBrief.model_validate_json(response.text)
    except ValidationError as exc:
        raise RuntimeError(f"Gemini JSON校验失败: {exc}") from exc
    return post_validate_brief(brief, candidates, credits_used, search_errors, stage_summary)


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
        "config": {"update_multi": True, "summary": {"content": compact_text(summary, 180)}},
        "header": {
            "title": {"tag": "plain_text", "content": compact_text(title, 80)},
            "subtitle": {"tag": "plain_text", "content": compact_text(subtitle, 80)},
            "template": template,
            "padding": "12px 12px 12px 12px",
        },
        "body": {"direction": "vertical", "padding": "12px 12px 12px 12px", "elements": elements},
    }


def safe_link(url: str) -> str:
    url = str(url).strip()
    return url.replace("'", "%27") if url.startswith(("http://", "https://")) else ""


def build_feishu_cards(brief: MorningBrief) -> list[dict]:
    groups = [(kind, [item for item in brief.items if item.content_type == kind]) for kind in ("今日新讯", "近期洞察", "经典方法")]
    group_chunks = [(kind, items[i : i + 3]) for kind, items in groups for i in range(0, len(items), 3)]
    total_cards = 2 + len(group_chunks)
    counts = {kind: len(items) for kind, items in groups}
    cards: list[dict] = []
    cards.append(
        make_card(
            f"设计情报早报 · {brief.report_date}",
            f"新讯{counts['今日新讯']} · 洞察{counts['近期洞察']} · 方法{counts['经典方法']} · 1/{total_cards}",
            brief.headline,
            [
                markdown_element(f"**今日核心判断**\n{compact_text(brief.headline, 180)}"),
                markdown_element(f"**执行摘要**\n{compact_text(brief.executive_summary, 420)}"),
                markdown_element("**时间层说明**\n今日新讯关注最新变化；近期洞察补充近6个月案例；经典方法明确标注旧资料及其当下价值。"),
            ],
            "blue",
        )
    )
    color = {"今日新讯": "turquoise", "近期洞察": "green", "经典方法": "purple"}
    for card_index, (kind, chunk) in enumerate(group_chunks, start=2):
        elements: list[dict] = []
        for item in chunk:
            link = safe_link(str(item.source_url))
            primary = "原始来源" if item.is_primary_source else "二手来源"
            elements.append(
                markdown_element(
                    f"**[{item.category}] {compact_text(item.title_cn, 88)}**  `{item.score}/100`\n"
                    f"{compact_text(item.summary, 240)}\n"
                    f"**为什么重要：** {compact_text(item.why_it_matters, 180)}\n"
                    f"**为什么现在：** {compact_text(item.why_now, 180)}\n"
                    f"**团队动作：** {compact_text(item.team_action, 150)}\n"
                    f"{item.published_date} · {item.time_horizon} · {compact_text(item.source_name, 40)} · {item.source_tier}级/{primary} · <a href='{link}'>查看原文</a>"
                )
            )
        cards.append(
            make_card(kind, f"{len(chunk)}条 · {card_index}/{total_cards}", f"设计早报{kind}", elements, color[kind])
        )
    if brief.visual_radar:
        radar = "\n".join(
            f"- **{compact_text(signal.signal, 110)}**：{compact_text(signal.evidence, 150)}（{compact_text(signal.source_name, 35)} · {signal.published_date} · <a href='{safe_link(str(signal.source_url))}'>来源</a>）"
            for signal in brief.visual_radar
        )
    else:
        radar = "- 本期没有足够的可核验证据，暂不形成视觉趋势判断。"
    follow = "\n".join(f"- {compact_text(item, 150)}" for item in brief.follow_ups[:4]) or "- 暂无。"
    cards.append(
        make_card(
            "视觉雷达与后续追踪",
            f"收尾 · {total_cards}/{total_cards}",
            "视觉趋势、追踪问题与方法说明",
            [
                markdown_element(f"**视觉趋势雷达**\n{radar}"),
                markdown_element(f"**建议继续追踪**\n{follow}"),
                markdown_element(f"**方法与局限**\n{compact_text(brief.methodology_note, 500)}"),
            ],
            "purple",
        )
    )
    for index, card in enumerate(cards, start=1):
        payload = {"msg_type": "interactive", "card": card}
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > FEISHU_SAFE_REQUEST_BYTES:
            raise RuntimeError(f"第{index}张卡片请求体{size} bytes，超过安全上限{FEISHU_SAFE_REQUEST_BYTES}")
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
    response = requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json; charset=utf-8"}, timeout=30)
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"飞书返回非JSON: {response.text[:300]}") from exc
    code = result.get("code", result.get("StatusCode", 0))
    if code not in (0, "0", None):
        raise RuntimeError(f"飞书发送失败: {result}")
    return result


def update_history(brief: MorningBrief) -> None:
    history = load_history()
    today = brief.report_date
    for item in brief.items:
        history.append(
            {
                "url": normalize_url(str(item.source_url)),
                "title": item.title_cn,
                "content_type": item.content_type,
                "selected_date": today,
            }
        )
    cutoff = local_today() - timedelta(days=max(HISTORY_CLASSIC_DAYS, 365))
    cleaned: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in reversed(history):
        try:
            selected = date.fromisoformat(str(entry.get("selected_date")))
        except ValueError:
            continue
        key = (normalize_url(str(entry.get("url", ""))), str(entry.get("selected_date")))
        if selected < cutoff or key in seen:
            continue
        seen.add(key)
        cleaned.append(entry)
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(list(reversed(cleaned)), ensure_ascii=False, indent=2), encoding="utf-8")


def save_outputs(brief: MorningBrief, candidates: list[dict], cards: list[dict], errors: list[str], rejected: list[dict], stage_summary: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    (OUTPUT_DIR / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "rejected-candidates.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "search-errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "search-stages.json").write_text(json.dumps(stage_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    for index, card in enumerate(cards, start=1):
        (OUTPUT_DIR / f"card-{index}.json").write_text(
            json.dumps({"msg_type": "interactive", "card": card}, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def demo_brief() -> MorningBrief:
    today = local_today().isoformat()
    return MorningBrief(
        report_date=today,
        headline="这是连通性测试卡片，不包含真实情报。",
        executive_summary="此消息用于验证GitHub Actions、签名校验与飞书Webhook是否连接成功。真实运行会分层检索今日新讯、近期洞察和经典方法。",
        items=[
            BriefItem(
                title_cn="飞书消息卡片连接测试",
                title_original="Feishu card connectivity test",
                content_type="今日新讯",
                time_horizon="测试",
                category="政策、奖项与机构",
                source_name="Feishu Open Platform",
                source_type="专业机构",
                source_tier="A",
                is_primary_source=True,
                source_url="https://open.feishu.cn/",
                published_date=today,
                summary="程序已生成符合飞书卡片结构的测试内容，并准备通过自定义机器人Webhook发送。",
                why_it_matters="证明云端运行环境能够读取仓库Secrets并与飞书机器人通信。",
                why_now="在启用每日自动推送前先确认基础连接可靠。",
                team_action="确认群聊收到卡片后，再运行完整检索与分析流程。",
                score=100,
                confidence="高",
            )
        ],
        visual_radar=[],
        follow_ups=["检查卡片是否完整显示。", "确认链接能正常打开。"],
        methodology_note="测试模式：未调用Tavily与Gemini，不消耗搜索或模型额度。",
    )


def run_full(send: bool = True) -> None:
    secrets = require_env(["TAVILY_API_KEY", "GEMINI_API_KEY"])
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    webhook_secret = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip() or None
    if send and not webhook_url:
        raise RuntimeError("发送模式缺少FEISHU_WEBHOOK_URL")
    candidates, search_errors, credits_used, rejected, stage_summary = run_searches(secrets["TAVILY_API_KEY"])
    LOGGER.info("Verified candidates=%s rejected=%s credits=%s", len(candidates), len(rejected), credits_used)
    brief = analyze_with_gemini(secrets["GEMINI_API_KEY"], candidates, search_errors, credits_used, stage_summary)
    cards = build_feishu_cards(brief)
    save_outputs(brief, candidates, cards, search_errors, rejected, stage_summary)
    update_history(brief)
    if send:
        for index, card in enumerate(cards, start=1):
            send_card(webhook_url, card, webhook_secret)
            LOGGER.info("Sent Feishu card %s/%s", index, len(cards))
            time.sleep(0.6)
    else:
        LOGGER.info("Dry run completed; no Feishu message sent")


def run_test_card() -> None:
    secrets = require_env(["FEISHU_WEBHOOK_URL"])
    secret = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip() or None
    brief = demo_brief()
    cards = build_feishu_cards(brief)
    save_outputs(brief, [], cards, [], [], {"test": {"credits": 0}})
    for index, card in enumerate(cards, start=1):
        send_card(secrets["FEISHU_WEBHOOK_URL"], card, secret)
        LOGGER.info("Sent test card %s/%s", index, len(cards))
        time.sleep(0.6)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="自动生成并发送全球设计情报与知识早报")
    parser.add_argument(
        "--mode",
        choices=["full", "dry-run", "test-feishu"],
        default="full",
        help="full=完整运行并发送；dry-run=生成文件但不发送；test-feishu=只测试飞书连接",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOGGER.info("Running code version: %s", CODE_VERSION)
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
