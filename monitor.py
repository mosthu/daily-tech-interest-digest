#!/usr/bin/env python3
"""Collect China-market consumer NVIDIA/AMD GPU prices and send a PushPlus digest."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / "data" / "price_history.json"
TZ_CN = timezone(timedelta(hours=8))

# GPU chip families to monitor. The report shows the lowest new listing found
# for each family, rather than flooding the message with every board partner SKU.
GPU_MODELS = [
    # NVIDIA GeForce RTX 50 / 40 / 30 / 16
    ("NVIDIA", "GeForce RTX 5090"), ("NVIDIA", "GeForce RTX 5090D"),
    ("NVIDIA", "GeForce RTX 5080"), ("NVIDIA", "GeForce RTX 5070 Ti"),
    ("NVIDIA", "GeForce RTX 5070"), ("NVIDIA", "GeForce RTX 5060 Ti 16GB"),
    ("NVIDIA", "GeForce RTX 5060 Ti 8GB"), ("NVIDIA", "GeForce RTX 5060"),
    ("NVIDIA", "GeForce RTX 5050"),
    ("NVIDIA", "GeForce RTX 4090"), ("NVIDIA", "GeForce RTX 4090D"),
    ("NVIDIA", "GeForce RTX 4080 SUPER"), ("NVIDIA", "GeForce RTX 4080"),
    ("NVIDIA", "GeForce RTX 4070 Ti SUPER"), ("NVIDIA", "GeForce RTX 4070 Ti"),
    ("NVIDIA", "GeForce RTX 4070 SUPER"), ("NVIDIA", "GeForce RTX 4070"),
    ("NVIDIA", "GeForce RTX 4060 Ti 16GB"), ("NVIDIA", "GeForce RTX 4060 Ti 8GB"),
    ("NVIDIA", "GeForce RTX 4060"), ("NVIDIA", "GeForce RTX 3090 Ti"),
    ("NVIDIA", "GeForce RTX 3090"), ("NVIDIA", "GeForce RTX 3080 Ti"),
    ("NVIDIA", "GeForce RTX 3080"), ("NVIDIA", "GeForce RTX 3070 Ti"),
    ("NVIDIA", "GeForce RTX 3070"), ("NVIDIA", "GeForce RTX 3060 Ti"),
    ("NVIDIA", "GeForce RTX 3060 12GB"), ("NVIDIA", "GeForce RTX 3060 8GB"),
    ("NVIDIA", "GeForce RTX 3050 8GB"), ("NVIDIA", "GeForce RTX 3050 6GB"),
    ("NVIDIA", "GeForce GTX 1660 SUPER"), ("NVIDIA", "GeForce GTX 1660"),
    ("NVIDIA", "GeForce GTX 1650"),
    # AMD Radeon RX 9000 / 7000 / 6000
    ("AMD", "Radeon RX 9070 XT"), ("AMD", "Radeon RX 9070"),
    ("AMD", "Radeon RX 9060 XT 16GB"), ("AMD", "Radeon RX 9060 XT 8GB"),
    ("AMD", "Radeon RX 7900 XTX"), ("AMD", "Radeon RX 7900 XT"),
    ("AMD", "Radeon RX 7900 GRE"), ("AMD", "Radeon RX 7800 XT"),
    ("AMD", "Radeon RX 7700 XT"), ("AMD", "Radeon RX 7600 XT"),
    ("AMD", "Radeon RX 7600"), ("AMD", "Radeon RX 6950 XT"),
    ("AMD", "Radeon RX 6900 XT"), ("AMD", "Radeon RX 6800 XT"),
    ("AMD", "Radeon RX 6800"), ("AMD", "Radeon RX 6750 XT"),
    ("AMD", "Radeon RX 6700 XT"), ("AMD", "Radeon RX 6650 XT"),
    ("AMD", "Radeon RX 6600 XT"), ("AMD", "Radeon RX 6600"),
    ("AMD", "Radeon RX 6500 XT"), ("AMD", "Radeon RX 6400"),
]

NEWS_FEEDS = {
    "Tom's Hardware": "https://www.tomshardware.com/feeds.xml",
    "TechPowerUp": "https://www.techpowerup.com/rss/news",
    "VideoCardz": "https://videocardz.com/feed",
}
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
CHINESE_TEXT = re.compile(r"[\u4e00-\u9fff]")
NEWS_KEYWORDS = (
    "gpu", "graphics card", "graphics cards", "geforce", "radeon", "rtx", "rx ",
    "cpu", "processor", "ryzen", "intel", "core ultra", "memory", "dram", "ddr4",
    "ddr5", "vram", "nand", "ssd", "price", "pricing", "shortage", "supply",
)

NEW_PLATFORM_URLS = {
    "京东": "https://search.jd.com/Search?keyword={query}&enc=utf-8",
    "淘宝": "https://s.taobao.com/search?q={query}",
    "拼多多": "https://mobile.yangkeduo.com/search_result.html?search_key={query}",
    # Douyin's public search pages are frequently JS-only or require login.
    "抖音": "https://www.douyin.com/search/{query}",
}
USED_PLATFORM_URLS = {
    "闲鱼": "https://www.goofish.com/search?q={query}",
    "转转": "https://www.zhuanzhuan.com/search?keyword={query}",
}
LISTED_PRICE = "公开挂牌"
SOLD_REFERENCE_PRICE = "已售/已成交参考"
SOLD_MARKERS = ("已售", "已成交", "已卖出", "交易成功", "sold", "sold out")


@dataclass
class Listing:
    brand: str
    gpu_model: str
    name: str
    price: float
    retailer: str
    variant: str
    url: str
    captured_at: str
    platform: str = "京东"
    market_type: str = "全新"
    price_kind: str = LISTED_PRICE


@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    published_at: str
    summary: str
    title_zh: str = ""
    summary_zh: str = ""


def compact(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def model_aliases(model: str) -> set[str]:
    compact_model = compact(model)
    aliases = {compact_model}
    aliases.add(compact_model.replace("GEFORCE", "").replace("RADEON", ""))
    aliases.update(alias.replace("GB", "G") for alias in list(aliases))
    return {alias for alias in aliases if alias}


def model_from_name(name: str) -> tuple[str, str] | None:
    # Check longer names first so RTX 4070 Ti SUPER is not classified as RTX 4070.
    name_compact = compact(name)
    for brand, model in sorted(GPU_MODELS, key=lambda row: len(compact(row[1])), reverse=True):
        if any(alias in name_compact for alias in model_aliases(model)):
            return brand, model
    return None


def classify_variant(name: str) -> str:
    special_words = (
        "红魔", "Red Devil", "NITRO", "超白金", "Taichi", "ROG", "猛禽",
        "水冷", "Liquid", "白色", "白魔", "限量", "旗舰", "AORUS",
        "SUPRIM", "AMP", "VANGUARD", "OC", "超频",
    )
    if any(word.lower() in name.lower() for word in special_words):
        return "特殊/高端非公版"
    return "标准/主流非公版"


def clean_text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def has_sold_marker(value: str) -> bool:
    text = (value or "").lower()
    return any(marker in text for marker in SOLD_MARKERS)


def parse_price(value: str) -> float | None:
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)", value.replace("￥", ""))
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def xml_value(element: ElementTree.Element, names: tuple[str, ...]) -> str:
    for child in list(element):
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in names and child.text:
            return unescape(child.text.strip())
    return ""


def parse_news_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(TZ_CN)
    except (TypeError, ValueError, OverflowError):
        return None


def strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True))


def translate_to_chinese(text: str, session: requests.Session) -> str:
    """Translate an English news field to Simplified Chinese when needed.

    The public endpoint does not require another secret. Translation is best-effort:
    if the service is unavailable, the original English text is returned so the
    daily report still goes out.
    """
    value = " ".join((text or "").split())
    if not value or CHINESE_TEXT.search(value):
        return value
    try:
        response = session.get(
            TRANSLATE_URL,
            params={"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": value[:1200]},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        translated = "".join(part[0] for part in payload[0] if part and part[0])
        return translated.strip() or value
    except (IndexError, KeyError, TypeError, ValueError, requests.RequestException):
        return value


def fetch_news(session: requests.Session) -> tuple[list[NewsItem], list[str]]:
    cutoff = datetime.now(TZ_CN) - timedelta(hours=48)
    items: list[NewsItem] = []
    errors: list[str] = []
    for source, feed_url in NEWS_FEEDS.items():
        try:
            response = session.get(feed_url, timeout=25)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
            for entry in entries:
                title = xml_value(entry, ("title",))
                link = xml_value(entry, ("link",))
                if not link:
                    for child in list(entry):
                        if child.tag.rsplit("}", 1)[-1].lower() == "link":
                            link = child.attrib.get("href", "")
                            break
                published = xml_value(entry, ("pubdate", "published", "updated", "date"))
                published_dt = parse_news_date(published)
                summary = strip_html(xml_value(entry, ("description", "summary", "content")))
                searchable = f"{title} {summary}".lower()
                if not title or not any(word in searchable for word in NEWS_KEYWORDS):
                    continue
                if published_dt and published_dt < cutoff:
                    continue
                short_summary = summary[:240]
                items.append(
                    NewsItem(
                        source,
                        title,
                        urljoin(feed_url, link),
                        published_dt.isoformat(timespec="minutes") if published_dt else "时间未知",
                        short_summary,
                        translate_to_chinese(title, session),
                        translate_to_chinese(short_summary, session),
                    )
                )
        except (requests.RequestException, ElementTree.ParseError) as exc:
            errors.append(f"{source}: {exc.__class__.__name__}")

    unique: dict[str, NewsItem] = {}
    for item in items:
        key = re.sub(r"[^a-z0-9]+", " ", item.title.lower()).strip()
        unique.setdefault(key, item)
    result = list(unique.values())
    result.sort(key=lambda item: item.published_at, reverse=True)
    return result[:8], errors


def trend_outlook(news: list[NewsItem]) -> str:
    categories = {
        "显卡": ("gpu", "graphics", "geforce", "radeon", "rtx", "rx "),
        "内存": ("memory", "dram", "ddr4", "ddr5", "vram", "nand"),
        "CPU": ("cpu", "processor", "ryzen", "intel", "core ultra"),
    }
    up_words = ("shortage", "tight supply", "limited stock", "price hike", "price increase", "rise", "higher", "cost", "tariff", "demand", "memory crunch")
    down_words = ("price cut", "price drop", "discount", "sale", "clearance", "oversupply", "weak demand", "fall", "lower", "cheaper")
    lines = ["### 后续价格趋势判断"]
    for category, words in categories.items():
        score = 0
        evidence: list[str] = []
        for item in news:
            text = f"{item.title} {item.summary}".lower()
            if not any(word in text for word in words):
                continue
            up = sum(text.count(word) for word in up_words)
            down = sum(text.count(word) for word in down_words)
            score += min(up, 3) - min(down, 3)
            if up or down:
                evidence.append(item.title[:42])
        direction = "偏涨" if score >= 2 else "偏跌" if score <= -2 else "震荡"
        if evidence:
            lines.append(f"- **{category}：{direction}**。依据：{evidence[0]}。")
        else:
            lines.append(f"- **{category}：震荡**。目前抓到的新闻缺少足够的直接价格信号。")
    lines.append("\n> 判断是基于新闻标题/摘要中的供需与价格信号做的自动研判，不能替代实际报价；显卡和内存价格通常比 CPU 更容易受到库存和促销影响。")
    return "\n".join(lines)


def jd_search(brand: str, model: str, session: requests.Session) -> list[Listing]:
    keyword = f"{model} 显卡"
    url = f"https://search.jd.com/Search?keyword={quote(keyword)}&enc=utf-8"
    response = session.get(url, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    captured_at = datetime.now(TZ_CN).isoformat(timespec="seconds")
    listings: list[Listing] = []

    for card in soup.select("li.gl-item"):
        name = clean_text(card.select_one(".p-name"))
        price = parse_price(clean_text(card.select_one(".p-price")))
        found = model_from_name(name) if name else None
        if not name or price is None or found != (brand, model):
            continue
        sku = card.get("data-sku", "")
        link = f"https://item.jd.com/{sku}.html" if sku else url
        shop = clean_text(card.select_one(".p-shop")) or "京东"
        listings.append(Listing(brand, model, name, price, shop, classify_variant(name), link, captured_at, "京东", "全新"))
        if has_sold_marker(clean_text(card)):
            listings.append(
                Listing(
                    brand,
                    model,
                    f"{name}（页面已售/已成交样本）",
                    price,
                    shop,
                    classify_variant(name),
                    link,
                    captured_at,
                    "京东",
                    "全新",
                    SOLD_REFERENCE_PRICE,
                )
            )
    return listings


def extract_platform_price(html: str) -> float | None:
    # Prefer values attached to price-like JSON keys; fall back to visible RMB text.
    candidates: list[float] = []
    price_patterns = (
        r"(?:price|salePrice|sale_price|displayPrice|priceInfo)\s*[\"']?\s*[:=]\s*[\"']?(\d{2,6}(?:\.\d{1,2})?)",
        r"[¥￥]\s*(\d{2,6}(?:,\d{3})?(?:\.\d{1,2})?)",
    )
    for pattern in price_patterns:
        for value in re.findall(pattern, html, flags=re.IGNORECASE):
            number = float(value.replace(",", ""))
            # A graphics card listing is unlikely to be below ¥100 or above ¥50,000.
            if 100 <= number <= 50000:
                candidates.append(number)
    return min(candidates) if candidates else None


def generic_platform_search(
    platform: str,
    brand: str,
    model: str,
    session: requests.Session,
    market_type: str,
) -> list[Listing]:
    base_urls = NEW_PLATFORM_URLS if market_type == "全新" else USED_PLATFORM_URLS
    template = base_urls[platform]
    query = f"{model} {'全新' if market_type == '全新' else '二手'}"
    search_url = template.format(query=quote(query))
    response = session.get(search_url, timeout=25, allow_redirects=True)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower() and not response.text.lstrip().startswith("<"):
        return []
    price = extract_platform_price(response.text)
    if price is None:
        return []
    captured_at = datetime.now(TZ_CN).isoformat(timespec="seconds")
    label = f"{model}（{platform}搜索最低参考价）"
    listings = [Listing(brand, model, label, price, "平台搜索结果", classify_variant(label), search_url, captured_at, platform, market_type)]
    if has_sold_marker(response.text):
        sold_label = f"{model}（{platform}页面已售/已成交参考价）"
        listings.append(
            Listing(
                brand,
                model,
                sold_label,
                price,
                "平台搜索结果",
                classify_variant(sold_label),
                search_url,
                captured_at,
                platform,
                market_type,
                SOLD_REFERENCE_PRICE,
            )
        )
    return listings


def representatives(listings: Iterable[Listing]) -> list[Listing]:
    # One lowest-price listing per GPU family, platform and price layer keeps the digest readable.
    cheapest: dict[tuple[str, str, str, str, str], Listing] = {}
    for item in listings:
        key = (item.brand, item.gpu_model, item.platform, item.market_type, item.price_kind)
        if key not in cheapest or item.price < cheapest[key].price:
            cheapest[key] = item
    return sorted(cheapest.values(), key=lambda x: (x.brand, x.price))


def load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history: list[dict]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(TZ_CN) - timedelta(days=180)
    kept = []
    for row in history:
        try:
            when = datetime.fromisoformat(row["captured_at"])
            if when >= cutoff:
                kept.append(row)
        except (KeyError, ValueError):
            continue
    HISTORY_PATH.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")


def recent_low(
    history: list[dict],
    brand: str,
    gpu_model: str,
    platform: str,
    market_type: str,
    price_kind: str,
    days: int,
) -> float | None:
    cutoff = datetime.now(TZ_CN) - timedelta(days=days)
    prices = []
    for row in history:
        if row.get("brand") != brand or row.get("gpu_model") != gpu_model:
            continue
        if row.get("platform", "京东") != platform or row.get("market_type", "全新") != market_type:
            continue
        if row.get("price_kind", LISTED_PRICE) != price_kind:
            continue
        try:
            when = datetime.fromisoformat(row["captured_at"])
            if when >= cutoff:
                prices.append(float(row["price"]))
        except (KeyError, ValueError, TypeError):
            pass
    return min(prices) if prices else None


def price_cell(item: Listing | None, history: list[dict]) -> str:
    if item is None:
        return "—"
    low30 = recent_low(history, item.brand, item.gpu_model, item.platform, item.market_type, item.price_kind, 30)
    low30_text = f"，30日低¥{low30:,.0f}" if low30 is not None else ""
    return f"[¥{item.price:,.0f}]({item.url})（{item.variant}{low30_text}）"


def comparison_table(listings: list[Listing], history: list[dict], market_type: str, price_kind: str) -> list[str]:
    platforms = list(NEW_PLATFORM_URLS) if market_type == "全新" else list(USED_PLATFORM_URLS)
    matrix = {
        (item.brand, item.gpu_model, item.platform): item
        for item in listings
        if item.market_type == market_type and item.price_kind == price_kind
    }
    keys = [(brand, model) for brand, model in GPU_MODELS if any((brand, model, p) in matrix for p in platforms)]
    if not keys:
        return ["暂无可用数据。"]
    header = "| 品牌 | GPU型号 | " + " | ".join(platforms) + " | 最低平台 |"
    divider = "|---|---|" + "---:|" * len(platforms) + "---|"
    rows = [header, divider]
    for brand, model in keys:
        items = [matrix.get((brand, model, platform)) for platform in platforms]
        available = [item for item in items if item is not None]
        lowest = min(available, key=lambda item: item.price) if available else None
        cells = [price_cell(item, history) for item in items]
        rows.append(f"| {brand} | {model} | " + " | ".join(cells) + f" | {lowest.platform if lowest else '—'} |")
    return rows


def markdown_report(listings: list[Listing], history: list[dict], news: list[NewsItem], statuses: dict[str, int]) -> str:
    today = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    title = "NVIDIA & AMD 消费级显卡全网价格日报"
    if not listings:
        report = f"## {title}\n\n日期：{today}\n\n本次未抓到可用平台价格，可能是页面结构变化、登录验证或访问限制。"
        return report + news_section(news)

    listed_count = sum(1 for item in listings if item.price_kind == LISTED_PRICE)
    sold_count = sum(1 for item in listings if item.price_kind == SOLD_REFERENCE_PRICE)
    rows = [
        f"## {title}\n\n日期：{today}\n",
        f"本次抓到 {listed_count} 条公开挂牌价、{sold_count} 条已售/已成交参考价。\n",
        "### 第一层：公开挂牌最低价",
        "#### 全新显卡：京东 / 淘宝 / 拼多多 / 抖音",
    ]
    rows.extend(comparison_table(listings, history, "全新", LISTED_PRICE))
    rows.append("\n#### 二手显卡：闲鱼 / 转转")
    rows.extend(comparison_table(listings, history, "二手", LISTED_PRICE))
    rows.extend(
        [
            "\n### 第二层：已售/已成交参考价",
            "> 仅统计页面明确出现“已售、已成交、已卖出、交易成功”等标记的公开样本，不代表平台全网真实最低实付价。",
            "#### 全新显卡：京东 / 淘宝 / 拼多多 / 抖音",
        ]
    )
    rows.extend(comparison_table(listings, history, "全新", SOLD_REFERENCE_PRICE))
    rows.append("\n#### 二手显卡：闲鱼 / 转转")
    rows.extend(comparison_table(listings, history, "二手", SOLD_REFERENCE_PRICE))
    status_text = "；".join(f"{platform}：{count}条" for platform, count in statuses.items())
    rows.append(f"\n> 平台采集状态：{status_text}。`—` 表示本次没有抓到可验证的公开价格或成交标记。公开挂牌价可能不含券、补贴、运费或议价空间。")
    rows.append("> 二手挂牌价和已售样本价都不等于平台全网最终成交最低价；请重点核对成色、维修史、矿卡风险、序列号和售后。")
    return "\n".join(rows) + news_section(news)


def markdown_title(value: str) -> str:
    """Keep RSS titles from accidentally breaking the Markdown link."""
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def news_section(news: list[NewsItem]) -> str:
    rows = ["\n\n## 最近硬件新闻"]
    if not news:
        return "\n".join(rows + ["\n暂未抓到过去48小时内符合条件的硬件新闻。", trend_outlook(news)])
    for number, item in enumerate(news, start=1):
        title = item.title_zh or item.title
        summary_text = item.summary_zh or item.summary
        summary_text = summary_text or "暂无摘要。"
        rows.extend(
            [
                f"\n### {number}. [{markdown_title(title)}]({item.url})",
                f"来源：{item.source}　发布时间：{item.published_at}",
                "",
                summary_text,
                "",
                f"[阅读原文]({item.url})",
                "\n---",
            ]
        )
    rows.append("\n" + trend_outlook(news))
    return "\n".join(rows)


def plain_text_report(markdown: str) -> str:
    """Convert Markdown into readable plain text for the ClawBot channel."""
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", markdown)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("`", "")
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\|?[\s:|-]+\|?", stripped):
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            line = " ｜ ".join(part.strip() for part in stripped.strip("|").split("|"))
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def pushplus_send(token: str, content: str, template: str, channel: str | None = None) -> None:
    payload = {
        "token": token,
        "title": "NVIDIA & AMD 显卡价格日报",
        "content": content,
        "template": template,
    }
    if channel:
        payload["channel"] = channel
    response = requests.post(
        "https://www.pushplus.plus/send",
        json=payload,
        timeout=25,
    )
    response.raise_for_status()
    body = response.json()
    if str(body.get("code")) not in {"200", "0"}:
        raise RuntimeError(f"PushPlus 返回异常：{body}")


def send_pushplus(content: str) -> None:
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        raise RuntimeError("未设置 PUSHPLUS_TOKEN 环境变量")
    pushplus_send(token, content, "markdown")

    # ClawBot uses the same PushPlus account by default. A separate token can
    # be supplied when the ClawBot channel belongs to another account.
    clawbot_token = os.environ.get("PUSHPLUS_CLAWBOT_TOKEN") or token
    try:
        pushplus_send(clawbot_token, plain_text_report(content), "txt", "clawbot")
        print("PushPlus ClawBot 推送成功")
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        # ClawBot is an additional channel; keep the normal daily report alive
        # when the channel has not been activated or is temporarily unavailable.
        print(f"PushPlus ClawBot 推送失败（普通 PushPlus 已成功）：{exc}", file=sys.stderr)


def collect_market_listings(session: requests.Session) -> tuple[list[Listing], dict[str, int], list[str]]:
    all_listings: list[Listing] = []
    errors: list[str] = []
    statuses = {platform: 0 for platform in (*NEW_PLATFORM_URLS, *USED_PLATFORM_URLS)}
    for brand, model in GPU_MODELS:
        for platform in NEW_PLATFORM_URLS:
            try:
                if platform == "京东":
                    found = jd_search(brand, model, session)
                else:
                    found = generic_platform_search(platform, brand, model, session, "全新")
                all_listings.extend(found)
                statuses[platform] += len(found)
            except requests.RequestException as exc:
                errors.append(f"{platform}/{model}: {exc.__class__.__name__}")
        for platform in USED_PLATFORM_URLS:
            try:
                found = generic_platform_search(platform, brand, model, session, "二手")
                all_listings.extend(found)
                statuses[platform] += len(found)
            except requests.RequestException as exc:
                errors.append(f"{platform}/{model}: {exc.__class__.__name__}")
    return representatives(all_listings), statuses, errors


def main() -> int:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    listings, statuses, errors = collect_market_listings(session)
    news, news_errors = fetch_news(session)
    errors.extend(news_errors)
    history = load_history()
    history.extend(asdict(item) for item in listings)
    save_history(history)
    report = markdown_report(listings, history, news, statuses)
    if errors:
        report += "\n\n抓取提示：" + "；".join(errors[:5])
    send_pushplus(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
