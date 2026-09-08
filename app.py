import hashlib
import hmac
import html
import json
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from functools import wraps
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import feedparser
from official_sources import BUILTIN_SOURCES, fetch_official
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for

APP_PREFIX = "/digest"
TZ = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
DB_PATH = os.getenv("DATABASE_PATH", "/data/digest.db")
APP_SECRET = os.environ["APP_SECRET"]
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36"

app = Flask(__name__, static_url_path=f"{APP_PREFIX}/static")
app.secret_key = APP_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True)
run_lock = threading.Lock()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def secret_box():
    key = base64.urlsafe_b64encode(hashlib.sha256(APP_SECRET.encode()).digest())
    return Fernet(key)


def encrypt_secret(value):
    return secret_box().encrypt(value.encode()).decode()


def decrypt_secret(value):
    try:
        return secret_box().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("发件账号授权码无法解密，请重新添加该账号") from exc


def parse_email_addresses(value):
    addresses = []
    for address in re.split(r"[,;，；\s]+", (value or "").strip()):
        if address and address not in addresses:
            addresses.append(address.lower())
    return addresses


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sources (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('rss','web')),
          url TEXT NOT NULL,
          item_selector TEXT NOT NULL DEFAULT '',
          title_selector TEXT NOT NULL DEFAULT '',
          link_selector TEXT NOT NULL DEFAULT '',
          time_selector TEXT NOT NULL DEFAULT '',
          summary_selector TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
          guid TEXT NOT NULL,
          title TEXT NOT NULL,
          url TEXT NOT NULL,
          summary TEXT NOT NULL DEFAULT '',
          published_at TEXT NOT NULL,
          collected_at TEXT NOT NULL,
          UNIQUE(source_id, guid)
        );
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          window_start TEXT NOT NULL,
          window_end TEXT NOT NULL,
          status TEXT NOT NULL,
          item_count INTEGER NOT NULL DEFAULT 0,
          email_sent INTEGER NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sender_accounts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          label TEXT NOT NULL,
          email TEXT NOT NULL UNIQUE,
          smtp_host TEXT NOT NULL,
          smtp_port INTEGER NOT NULL DEFAULT 465,
          use_ssl INTEGER NOT NULL DEFAULT 1,
          secret_encrypted TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          is_default INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscribers (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          email TEXT NOT NULL UNIQUE,
          schedule_time TEXT NOT NULL DEFAULT '19:00',
          window_mode TEXT NOT NULL DEFAULT 'days',
          window_value INTEGER NOT NULL DEFAULT 1,
          enabled INTEGER NOT NULL DEFAULT 1,
          last_sent_at TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscriber_sources (
          subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
          source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
          PRIMARY KEY(subscriber_id, source_id)
        );
        """)
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        conn.execute('CREATE TABLE IF NOT EXISTS reader_pages (token TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL)')
        if "subscriber_id" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN subscriber_id INTEGER REFERENCES subscribers(id) ON DELETE SET NULL")
        defaults = {
            "smtp_host": "", "smtp_port": "465", "smtp_user": "",
            "smtp_from": "", "smtp_to": "", "smtp_ssl": "1",
            "email_subject": "每日消息汇总",
            "sender_name": "每日财经与Ai消息汇总",
            "llm_endpoint": "https://api.openai.com/v1/chat/completions",
            "llm_model": "", "llm_enabled": "0",
            "window_mode": "hours", "window_value": "24",
            "schedule_enabled": "0",
        }
        conn.executemany("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", defaults.items())
        for source_name, source_kind, source_url in BUILTIN_SOURCES:
            conn.execute("""INSERT INTO sources(name,kind,url,created_at)
                SELECT ?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM sources WHERE url=?)""",
                (source_name, source_kind, source_url, iso(now()), source_url))
        account_count = conn.execute("SELECT COUNT(*) FROM sender_accounts").fetchone()[0]
        legacy = get_settings(conn)
        legacy_secret = os.getenv("SMTP_PASSWORD", "")
        legacy_email = legacy.get("smtp_user") or legacy.get("smtp_from")
        if account_count == 0 and legacy_secret and legacy_email:
            conn.execute("""INSERT INTO sender_accounts
                (label,email,smtp_host,smtp_port,use_ssl,secret_encrypted,enabled,is_default,created_at)
                VALUES(?,?,?,?,?,?,1,1,?)""",
                ("QQ 邮箱", legacy_email, legacy.get("smtp_host") or "smtp.qq.com",
                 int(legacy.get("smtp_port") or 465), int(legacy.get("smtp_ssl") or 1),
                 encrypt_secret(legacy_secret), iso(now())))
        subscriber_count = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
        if subscriber_count == 0:
            for address in parse_email_addresses(legacy.get("smtp_to", "")):
                cur = conn.execute("""INSERT OR IGNORE INTO subscribers
                    (name,email,schedule_time,window_mode,window_value,enabled,created_at)
                    VALUES(?,?,?,?,?,1,?)""", (address.split("@", 1)[0], address, "19:00",
                    legacy.get("window_mode") or "days", int(legacy.get("window_value") or 1), iso(now())))
                subscriber_id = cur.lastrowid
                if subscriber_id:
                    conn.execute("""INSERT OR IGNORE INTO subscriber_sources(subscriber_id,source_id)
                        SELECT ?,id FROM sources WHERE enabled=1""", (subscriber_id,))


def now():
    return datetime.now(TZ)


def iso(dt):
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def parse_dt(value, fallback=None):
    if not value:
        return fallback
    value = str(value).strip()
    chinese = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2})[:时](\d{1,2})?)?", value)
    if chinese:
        year, month, day, hour, minute = chinese.groups()
        return datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), tzinfo=TZ)
    try:
        dt = date_parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError, OverflowError):
        return fallback


def digest_window(settings=None, anchor=None):
    anchor = (anchor or now()).astimezone(TZ)
    settings = settings or {"window_mode": "hours", "window_value": "24"}
    try:
        value = max(1, min(90, int(settings.get("window_value") or 1)))
    except ValueError:
        value = 1
    end = anchor.replace(microsecond=0)
    if settings.get("window_mode") == "days":
        start = (end - timedelta(days=value - 1)).replace(hour=0, minute=0, second=0)
    else:
        start = end - timedelta(hours=value)
    return start, end


def get_settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM settings")}


def clean_text(value, limit=1200):
    text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:limit]


def item_guid(url, title, published):
    raw = f"{url}|{title}|{published.isoformat()}".encode()
    return hashlib.sha256(raw).hexdigest()


def fetch_rss(source):
    response = requests.get(source["url"], timeout=25, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    results = []
    for entry in parsed.entries:
        published = parse_dt(entry.get("published") or entry.get("updated"))
        if not published:
            continue
        url = entry.get("link", "").strip()
        title = clean_text(entry.get("title", "无标题"), 300)
        guid = str(entry.get("id") or item_guid(url, title, published))
        results.append({"guid": guid, "title": title, "url": url,
                        "summary": clean_text(entry.get("summary", "")), "published": published})
    return results


def select_text(node, selector):
    found = node.select_one(selector) if selector else None
    return found.get_text(" ", strip=True) if found else ""


def fetch_web(source):
    required = (source["item_selector"], source["title_selector"], source["link_selector"])
    if not all(required):
        raise ValueError("网页来源缺少条目、标题或链接 CSS 选择器")
    response = requests.get(source["url"], timeout=25, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for node in soup.select(source["item_selector"]):
        link_node = node.select_one(source["link_selector"])
        if not link_node or not link_node.get("href"):
            continue
        title = select_text(node, source["title_selector"])
        url = urljoin(source["url"], link_node["href"])
        published = parse_dt(select_text(node, source["time_selector"]), now())
        summary = select_text(node, source["summary_selector"])
        stable_guid = hashlib.sha256(f"{url}|{title}".encode()).hexdigest()
        results.append({"guid": stable_guid, "title": clean_text(title, 300),
                        "url": url, "summary": clean_text(summary), "published": published})
    return results


def fetch_aiera(source):
    response = requests.get(source["url"], timeout=25, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for node in soup.select("main article"):
        link = node.select_one("h2 a[href]")
        published = parse_dt(select_text(node, "time"))
        if not link or not published:
            continue
        url = urljoin(source["url"], link["href"])
        title = clean_text(link.get_text(" ", strip=True), 300)
        summary = clean_text(select_text(node, "p"))
        results.append({"guid": hashlib.sha256(url.encode()).hexdigest(), "title": title,
                        "url": url, "summary": summary, "published": published})
    return results


def fetch_sina_detail(entry):
    title, url = entry
    try:
        response = requests.get(url, timeout=18, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        time_meta = (soup.select_one('meta[property="article:published_time"]') or
                     soup.select_one('meta[property="bytedance:published_time"]') or
                     soup.select_one('meta[name="weibo: article:create_at"]'))
        description = (soup.select_one('meta[name="description"]') or
                       soup.select_one('meta[property="og:description"]'))
        published = parse_dt(time_meta.get("content") if time_meta else None)
        summary = clean_text(description.get("content") if description else "")
    except Exception:
        published, summary = None, ""
    if not published:
        match = re.search(r"/(\d{4})-(\d{2})-(\d{2})/", url)
        published = datetime(*map(int, match.groups()), tzinfo=TZ) if match else now()
    return {"guid": hashlib.sha256(url.encode()).hexdigest(), "title": title, "url": url,
            "summary": summary, "published": published}


def fetch_sina(source):
    response = requests.get(source["url"], timeout=25, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    section = soup.select_one("section.important-news-area")
    if not section:
        raise ValueError("未找到新浪财经要闻栏目")
    entries, seen = [], set()
    for link in section.select("a[href]"):
        url = urljoin(source["url"], link["href"])
        title = clean_text(link.get_text(" ", strip=True), 300)
        if (not title or url in seen or "finance.sina.com.cn" not in urlparse(url).netloc
                or not re.search(r"/doc-[^/]+\.shtml", url)):
            continue
        seen.add(url)
        entries.append((title, url))
        if len(entries) >= 60:
            break
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_sina_detail, entry) for entry in entries]
        for future in as_completed(futures):
            results.append(future.result())
    return results


def cls_sign(params):
    query = "&".join(f"{key}={params[key]}" for key in sorted(params))
    sha1_hex = hashlib.sha1(query.encode()).hexdigest()
    return hashlib.md5(sha1_hex.encode()).hexdigest()


def cls_request(path, params, referer):
    params = {**params, "os": "web", "sv": "8.7.9", "app": "CailianpressWeb"}
    params["sign"] = cls_sign(params)
    response = requests.get("https://www.cls.cn" + path, params=params, timeout=25,
                            headers={"User-Agent": USER_AGENT, "Referer": referer,
                                     "Accept": "application/json, text/plain, */*"})
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("财联社接口返回了非 JSON 内容") from exc
    if payload.get("errno") != 0:
        raise ValueError(f"财联社接口错误：{payload.get('msg') or payload.get('errno')}")
    return payload.get("data")


def fetch_cls(source):
    match = re.search(r"[?&]id=(\d+)", source["url"])
    category_id = match.group(1) if match else "1003"
    data = cls_request(f"/v3/depth/home/assembled/{category_id}", {}, source["url"])
    rows = list((data or {}).get("depth_list") or [])
    for _ in range(2):
        if not rows:
            break
        page = cls_request(f"/v3/depth/list/{category_id}",
                           {"last_time": rows[-1].get("ctime", ""), "rn": 20, "id": category_id},
                           source["url"])
        if not isinstance(page, list) or not page:
            break
        known = {row.get("id") for row in rows}
        rows.extend(row for row in page if row.get("id") not in known)
    results = []
    for row in rows:
        try:
            published = datetime.fromtimestamp(int(row.get("ctime", 0)), TZ)
        except (TypeError, ValueError, OSError):
            continue
        url = row.get("external_link") or f"https://www.cls.cn/detail/{row.get('id')}"
        results.append({"guid": f"cls-{row.get('id')}", "title": clean_text(row.get("title", ""), 300),
                        "url": url, "summary": clean_text(row.get("brief", "")), "published": published})
    return results


def parse_relative_time(value, anchor=None):
    anchor = (anchor or now()).astimezone(TZ)
    text = (value or "").strip()
    match = re.search(r"(\d+)分钟前", text)
    if match:
        return anchor - timedelta(minutes=int(match.group(1)))
    match = re.search(r"(\d+)小时前", text)
    if match:
        return anchor - timedelta(hours=int(match.group(1)))
    match = re.search(r"(\d+)天前", text)
    if match:
        return anchor - timedelta(days=int(match.group(1)))
    match = re.search(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if match:
        month, day, hour, minute = map(int, match.groups())
        parsed = datetime(anchor.year, month, day, hour, minute, tzinfo=TZ)
        return parsed.replace(year=anchor.year - 1) if parsed > anchor + timedelta(days=1) else parsed
    return parse_dt(text, anchor)


def fetch_jin10(source):
    response = requests.get(source["url"], timeout=25, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for node in soup.select(".jin10-news-index-list .jin10-news-list-item.news"):
        link = node.select_one("a[href*='/details/']")
        title_node = node.select_one(".jin10-news-list-item-title")
        if not link or not title_node:
            continue
        url = urljoin(source["url"], link.get("href", ""))
        title = clean_text(title_node.get_text(" ", strip=True), 300)
        summary = clean_text(select_text(node, ".jin10-news-list-item-introduction"))
        time_node = node.select_one(".jin10-news-list-item-display_datetime > span")
        published = parse_relative_time(time_node.get_text(" ", strip=True) if time_node else "")
        data_id = node.get("data-id") or hashlib.sha256(url.encode()).hexdigest()
        results.append({"guid": f"jin10-{data_id}", "title": title, "url": url,
                        "summary": summary, "published": published})
    if not results:
        raise ValueError("未找到金十数据热点头条内容")
    return results


def collect_source(conn, source, start, end):
    host = urlparse(source["url"]).netloc.lower()
    if host in ('www.anthropic.com', 'www.pbc.gov.cn', 'www.csrc.gov.cn', 'www.stats.gov.cn', 'qwen.ai'):
        fetched = fetch_official(source, parse_dt, clean_text, USER_AGENT)
    elif "aiera.com.cn" in host:
        fetched = fetch_aiera(source)
    elif "cls.cn" in host:
        fetched = fetch_cls(source)
    elif "finance.sina.com.cn" in host:
        fetched = fetch_sina(source)
    elif "jin10.com" in host:
        fetched = fetch_jin10(source)
    else:
        fetched = fetch_rss(source) if source["kind"] == "rss" else fetch_web(source)
    count = 0
    for item in fetched:
        conn.execute("""INSERT OR IGNORE INTO items
            (source_id,guid,title,url,summary,published_at,collected_at) VALUES(?,?,?,?,?,?,?)""",
            (source["id"], item["guid"], item["title"] or "无标题", item["url"], item["summary"],
             iso(item["published"]), iso(now())))
        count += conn.execute("SELECT changes()").fetchone()[0]
    return count


def llm_overview(settings, items):
    if settings.get("llm_enabled") != "1" or not settings.get("llm_model") or not os.getenv("LLM_API_KEY"):
        return ""
    material = "\n".join(f"- [{i['source_name']}] {i['title']}: {i['summary'][:500]}" for i in items[:150])
    prompt = "请用中文将以下消息归纳为5-10条简洁要点。忠于原文，不臆测，不遗漏重要风险。\n" + material
    response = requests.post(settings["llm_endpoint"], timeout=90,
        headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}", "Content-Type": "application/json"},
        json={"model": settings["llm_model"], "messages": [{"role": "user", "content": prompt}], "temperature": 0.2})
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def classify_topic(item):
    text = f"{item['title']} {item['summary']}".lower()
    rules = (
        ("AI / 科技", ("ai", "人工智能", "模型", "机器人", "芯片", "算力", "openai", "大模型")),
        ("A股 / 市场", ("a股", "股价", "涨停", "跌停", "指数", "板块", "收盘", "证券", "科创板")),
        ("公司 / 产业", ("公司", "融资", "ipo", "上市", "业绩", "财报", "产业", "投资")),
        ("宏观 / 政策", ("央行", "政策", "经济", "利率", "汇率", "监管", "外交部", "通胀")),
    )
    for label, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return label
    return "综合要闻"


def render_email(start, end, items, overview, source_names=None):
    grouped = {}
    for item in items:
        grouped.setdefault(item["source_name"], []).append(item)
    grouped_entries = list(grouped.items())
    source_names = list(dict.fromkeys(source_names or grouped.keys()))
    anchor_by_source = {name: index for index, (name, _) in enumerate(grouped_entries)}
    colors = ("#176b4d", "#b14c32", "#315da8", "#7657a6")
    summary_cards = "".join(
        f'<td style="padding:6px"><div style="background:#fff;border:1px solid #e3e8e5;border-radius:10px;padding:12px 14px">'
        f'<div style="font-size:11px;color:#78817c">{html.escape(name)}</div><div style="font-size:22px;font-weight:750;color:#17221d">{len(rows)}</div></div></td>'
        for name, rows in grouped_entries)
    toc_parts = []
    for list_index, name in enumerate(source_names):
        rows = grouped.get(name, [])
        if rows:
            anchor_index = anchor_by_source[name]
            color = colors[anchor_index % len(colors)]
            toc_parts.append(
                f'<a href="#platform-{anchor_index}" style="display:inline-block;margin:4px 5px 4px 0;padding:9px 12px;'
                f'border:1px solid {color}35;border-radius:9px;background:{color}0d;color:{color};'
                f'font-size:13px;font-weight:750;text-decoration:none">{html.escape(name)} '
                f'<span style="font-size:11px;font-weight:600;opacity:.75">{len(rows)} 条</span></a>')
        else:
            toc_parts.append(
                f'<span style="display:inline-block;margin:4px 5px 4px 0;padding:9px 12px;border:1px solid #e1e5e3;'
                f'border-radius:9px;background:#f5f6f5;color:#8a928e;font-size:13px;font-weight:650">'
                f'{html.escape(name)} <span style="font-size:11px;font-weight:500">0 条 · 今日无更新</span></span>')
    toc_links = "".join(toc_parts)
    updated_count = len(grouped_entries)
    toc_html = (f'''<tr><td style="padding:20px 24px 4px"><div style="background:#fff;border:1px solid #e3e9e5;border-radius:12px;padding:15px 16px"><div style="font-size:11px;letter-spacing:1px;color:#7c8781;font-weight:800;margin-bottom:7px">订阅平台 {len(source_names)} 个 · {updated_count} 个有更新 · 点击快速跳转</div><div>{toc_links}</div></div></td></tr>''' if toc_links else "")
    sections = []
    for index, (source, rows) in enumerate(grouped_entries):
        color = colors[index % len(colors)]
        cards = []
        for item in rows:
            title = html.escape(item["title"])
            url = html.escape(item["url"], quote=True)
            summary = html.escape(item["summary"][:360])
            published = html.escape(item["published_at"][:16].replace("T", " "))
            topic = html.escape(classify_topic(item))
            cards.append(f'''<tr><td style="padding:0 0 12px"><table role="presentation" width="100%" style="background:#fff;border:1px solid #e6eae7;border-radius:12px;border-collapse:separate"><tr><td style="padding:16px 18px"><div style="margin-bottom:8px"><span style="display:inline-block;background:{color}14;color:{color};border-radius:99px;padding:3px 8px;font-size:11px;font-weight:700">{topic}</span><span style="float:right;color:#8a928e;font-size:11px">{published}</span></div><a href="{url}" style="color:#17221d;text-decoration:none;font-size:17px;line-height:1.45;font-weight:750">{title}</a>{f'<p style="margin:8px 0 10px;color:#5e6862;line-height:1.65;font-size:13px">{summary}</p>' if summary else ''}<a href="{url}" style="color:{color};font-size:12px;font-weight:700;text-decoration:none">阅读原文 →</a></td></tr></table></td></tr>''')
        sections.append(f'''<tr><td style="padding:24px 24px 4px"><a id="platform-{index}" name="platform-{index}" style="display:block;height:1px;overflow:hidden">&nbsp;</a><table role="presentation" width="100%"><tr><td style="border-left:4px solid {color};padding-left:10px;font-size:20px;font-weight:800">{html.escape(source)}</td><td align="right" style="color:#7b847f;font-size:12px">{len(rows)} 条&nbsp;&nbsp;·&nbsp;&nbsp;<a href="#digest-top" style="color:{color};text-decoration:none">返回顶部 ↑</a></td></tr></table></td></tr><tr><td style="padding:12px 24px"><table role="presentation" width="100%">{''.join(cards)}</table></td></tr>''')
    overview_html = ""
    if overview:
        overview_html = f'''<tr><td style="padding:20px 24px 0"><div style="background:#eaf3ef;border-radius:12px;padding:18px"><div style="font-size:12px;color:#176b4d;font-weight:800;margin-bottom:8px">AI 今日摘要</div><div style="color:#34413a;line-height:1.7;font-size:14px">{html.escape(overview).replace(chr(10), '<br>')}</div></div></td></tr>'''
    content = "".join(sections) if sections else '<tr><td style="padding:45px;text-align:center;color:#7b847f">本时间段没有采集到新消息。</td></tr>'
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;background:#eef1ef;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#17221d"><a id="digest-top" name="digest-top"></a><table role="presentation" width="100%" style="background:#eef1ef"><tr><td align="center" style="padding:24px 10px"><table role="presentation" width="100%" style="max-width:760px;background:#f8faf9;border-radius:18px;overflow:hidden;border-collapse:separate"><tr><td style="background:#17221d;padding:30px 28px;color:#fff"><div style="font-size:11px;letter-spacing:2px;color:#9fc4b5;font-weight:800">DAILY INTELLIGENCE</div><h1 style="font-size:28px;margin:7px 0 8px">每日消息汇总</h1><div style="color:#c8d4ce;font-size:13px">{start:%Y-%m-%d %H:%M} — {end:%Y-%m-%d %H:%M} · 北京时间</div></td></tr>{toc_html}<tr><td style="padding:14px 18px 0"><table role="presentation" width="100%"><tr><td style="padding:6px"><div style="background:#176b4d;color:#fff;border-radius:10px;padding:12px 14px"><div style="font-size:11px;color:#b9d7ca">本期收录</div><div style="font-size:22px;font-weight:800">{len(items)} 条</div></div></td>{summary_cards}</tr></table></td></tr>{overview_html}{content}<tr><td style="padding:24px;text-align:center;color:#929995;font-size:11px;border-top:1px solid #e3e7e4">由 Daily Digest 自动整理 · 原文版权归各平台所有</td></tr></table></td></tr></table></body></html>'''


def publish_reader_page(conn, body):
    token = secrets.token_urlsafe(32)
    reader_url = f'{PUBLIC_BASE_URL}{APP_PREFIX}/read/{token}'
    conn.execute('DELETE FROM reader_pages WHERE created_at<?', (iso(now() - timedelta(days=90)),))
    conn.execute('INSERT INTO reader_pages(token,body,created_at) VALUES(?,?,?)', (token, body, iso(now())))
    email_body = body.replace('href="#', 'href="' + reader_url + '#')
    marker = '<table role="presentation" width="100%" style="background:#eef1ef">'
    browser_link = ('<div style="text-align:center;padding:16px;background:#e5f5ee">'
                    '<a href="' + reader_url + '" style="color:#176b4d;font-size:16px">'
                    '在浏览器中阅读 · 按平台快速定位 →</a></div>')
    return reader_url, email_body.replace(marker, browser_link + marker, 1)


def send_email(settings, subject, body, account, recipient):
    required = [account, recipient]
    if not all(required):
        raise RuntimeError("发件账号或收件地址尚未配置")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient):
        raise RuntimeError("收件邮箱格式无效")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.get("sender_name") or "每日财经与Ai消息汇总", account["email"]))
    msg["To"] = recipient
    msg.attach(MIMEText(body, "html", "utf-8"))
    smtp_cls = smtplib.SMTP_SSL if account["use_ssl"] else smtplib.SMTP
    with smtp_cls(account["smtp_host"], int(account["smtp_port"]), timeout=30) as smtp:
        if not account["use_ssl"]:
            smtp.starttls()
        smtp.login(account["email"], decrypt_secret(account["secret_encrypted"]))
        smtp.send_message(msg, to_addrs=[recipient])


def execute_digest(send=True, account_id=None, subscriber_id=None, subscriber_ids=None, scheduled=False):
    if not run_lock.acquire(blocking=False):
        raise RuntimeError("已有汇总任务正在运行")
    current_run_id = None
    results = []
    try:
        with db() as conn:
            settings = get_settings(conn)
            if account_id:
                account = conn.execute("SELECT * FROM sender_accounts WHERE id=? AND enabled=1", (account_id,)).fetchone()
            else:
                account = conn.execute("""SELECT * FROM sender_accounts WHERE enabled=1
                    ORDER BY is_default DESC,id LIMIT 1""").fetchone()
            if subscriber_ids:
                normalized_ids = sorted({int(value) for value in subscriber_ids})
                placeholders = ",".join("?" for _ in normalized_ids)
                subscribers = conn.execute(
                    f"SELECT * FROM subscribers WHERE enabled=1 AND id IN ({placeholders}) ORDER BY id",
                    normalized_ids).fetchall()
            elif subscriber_id:
                subscribers = conn.execute("SELECT * FROM subscribers WHERE id=?", (subscriber_id,)).fetchall()
            else:
                subscribers = conn.execute("SELECT * FROM subscribers WHERE enabled=1 ORDER BY id").fetchall()
            if not subscribers:
                raise RuntimeError("尚未配置订阅用户")
            collected_sources = set()
            source_errors = {}
            for subscriber in subscribers:
                errors = []
                start, end = digest_window(dict(subscriber))
                cur = conn.execute("""INSERT INTO runs(started_at,window_start,window_end,status,subscriber_id)
                    VALUES(?,?,?,?,?)""", (iso(now()), iso(start), iso(end), "running", subscriber["id"]))
                current_run_id = cur.lastrowid
                sources = conn.execute("""SELECT sources.* FROM sources
                    JOIN subscriber_sources ON subscriber_sources.source_id=sources.id
                    WHERE subscriber_sources.subscriber_id=? AND sources.enabled=1 ORDER BY sources.id""",
                    (subscriber["id"],)).fetchall()
                for source in sources:
                    if source["id"] in collected_sources:
                        continue
                    if source["id"] in source_errors:
                        errors.append(f"{source['name']}: {source_errors[source['id']]}")
                        continue
                    try:
                        collect_source(conn, source, start, end)
                        collected_sources.add(source["id"])
                    except Exception as exc:
                        source_errors[source["id"]] = str(exc)
                        errors.append(f"{source['name']}: {exc}")
                source_ids = [source["id"] for source in sources]
                if source_ids:
                    placeholders = ",".join("?" for _ in source_ids)
                    items = conn.execute(f"""SELECT items.*,sources.name source_name FROM items
                        JOIN sources ON sources.id=items.source_id
                        WHERE items.source_id IN ({placeholders}) AND published_at>=? AND published_at<?
                        ORDER BY published_at DESC""", (*source_ids, iso(start), iso(end))).fetchall()
                else:
                    items = []
                    errors.append("未选择消息来源")
                overview = ""
                try:
                    overview = llm_overview(settings, items)
                except Exception as exc:
                    errors.append(f"AI 摘要: {exc}")
                body = render_email(start, end, items, overview,
                                    source_names=[source["name"] for source in sources])
                reader_url, body = publish_reader_page(conn, body)
                sent = 0
                if send:
                    try:
                        subject = f"{start:%Y-%m-%d %H:%M} — {end:%Y-%m-%d %H:%M}"
                        send_email(settings, subject, body, account, subscriber["email"])
                        sent = 1
                    except Exception as exc:
                        errors.append(f"邮件: {exc}")
                status = "success" if not errors else ("partial" if items else "failed")
                conn.execute("UPDATE runs SET finished_at=?,status=?,item_count=?,email_sent=?,error=? WHERE id=?",
                             (iso(now()), status, len(items), sent, "\n".join(errors)[:4000], current_run_id))
                if sent and scheduled:
                    conn.execute("UPDATE subscribers SET last_sent_at=? WHERE id=?", (iso(now()), subscriber["id"]))
                results.append({"run_id": current_run_id, "subscriber": subscriber["name"],
                                "email": subscriber["email"], "body": body, "errors": errors, "sent": sent})
                current_run_id = None
            return results
    except Exception as exc:
        if current_run_id:
            with db() as conn:
                conn.execute("UPDATE runs SET finished_at=?,status='failed',error=? WHERE id=?",
                             (iso(now()), str(exc), current_run_id))
        raise
    finally:
        run_lock.release()


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password, encoded):
    try:
        salt_hex, digest_hex = encoded.split(":", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 310000)
        return hmac.compare_digest(actual.hex(), digest_hex)
    except ValueError:
        return False


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


@app.before_request
def check_csrf():
    if request.method == "POST" and request.endpoint != "login":
        if not hmac.compare_digest(session.get("csrf", ""), request.form.get("csrf", "")):
            abort(400, "CSRF 校验失败")


@app.get("/health")
def health():
    return jsonify(status="ok", time=iso(now()))


@app.get(APP_PREFIX)
def digest_root_redirect():
    return redirect(url_for("dashboard"))


@app.route(f"{APP_PREFIX}/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = os.getenv("ADMIN_USERNAME", "admin")
        stored = os.getenv("ADMIN_PASSWORD_HASH")
        valid = verify_password(request.form.get("password", ""), stored) if stored else hmac.compare_digest(
            request.form.get("password", ""), os.getenv("ADMIN_PASSWORD", ""))
        if hmac.compare_digest(request.form.get("username", ""), username) and valid:
            session.clear(); session["logged_in"] = True; csrf_token()
            return redirect(url_for("dashboard"))
        flash("用户名或密码错误", "error")
    return render_template("login.html")


@app.post(f"{APP_PREFIX}/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get(f"{APP_PREFIX}/")
@login_required
def dashboard():
    page_size = 30
    page = max(1, request.args.get("page", 1, type=int))
    with db() as conn:
        sources = conn.execute("SELECT * FROM sources ORDER BY id DESC").fetchall()
        runs = conn.execute("""SELECT runs.*,subscribers.name subscriber_name FROM runs
            LEFT JOIN subscribers ON subscribers.id=runs.subscriber_id ORDER BY runs.id DESC LIMIT 20""").fetchall()
        settings = get_settings(conn)
        accounts = conn.execute("SELECT * FROM sender_accounts ORDER BY is_default DESC,id").fetchall()
        subscribers = conn.execute("SELECT * FROM subscribers ORDER BY enabled DESC,id").fetchall()
        subscriber_source_rows = conn.execute("""SELECT subscriber_id,source_id,sources.name FROM subscriber_sources
            JOIN sources ON sources.id=subscriber_sources.source_id ORDER BY sources.id""").fetchall()
        subscriber_sources = {}
        for row in subscriber_source_rows:
            subscriber_sources.setdefault(row["subscriber_id"], []).append(row)
        latest_total = conn.execute("""SELECT COUNT(*) FROM items
            JOIN sources ON sources.id=items.source_id""").fetchone()[0]
        total_pages = max(1, (latest_total + page_size - 1) // page_size)
        page = min(page, total_pages)
        latest = conn.execute("""SELECT items.*,sources.name source_name FROM items JOIN sources ON sources.id=items.source_id
            ORDER BY published_at DESC LIMIT ? OFFSET ?""", (page_size, (page - 1) * page_size)).fetchall()
    start, end = digest_window(settings)
    send_ready = bool(accounts and subscribers)
    return render_template("dashboard.html", sources=sources, runs=runs, settings=settings, accounts=accounts,
                           subscribers=subscribers, subscriber_sources=subscriber_sources,
                           latest=latest,
                           latest_total=latest_total, page=page, total_pages=total_pages,
                           window_start=start, window_end=end, smtp_ready=bool(accounts),
                           send_ready=send_ready,
                           llm_ready=bool(os.getenv("LLM_API_KEY")))


@app.post(f"{APP_PREFIX}/sources")
@login_required
def add_source():
    kind = request.form.get("kind", "rss")
    url = request.form.get("url", "").strip()
    if kind not in ("rss", "web") or urlparse(url).scheme not in ("http", "https"):
        abort(400, "来源类型或网址无效")
    fields = [request.form.get(k, "").strip() for k in
              ("item_selector", "title_selector", "link_selector", "time_selector", "summary_selector")]
    with db() as conn:
        conn.execute("""INSERT INTO sources(name,kind,url,item_selector,title_selector,link_selector,time_selector,
          summary_selector,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)""",
          (request.form.get("name", "").strip() or urlparse(url).netloc, kind, url, *fields, iso(now())))
    flash("来源已添加", "success")
    return redirect(url_for("dashboard"))


@app.post(f"{APP_PREFIX}/sources/<int:source_id>/toggle")
@login_required
def toggle_source(source_id):
    with db() as conn:
        conn.execute("UPDATE sources SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (source_id,))
    return redirect(url_for("dashboard"))


@app.post(f"{APP_PREFIX}/sources/<int:source_id>/delete")
@login_required
def delete_source(source_id):
    with db() as conn:
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    flash("来源及其历史条目已删除", "success")
    return redirect(url_for("dashboard"))


@app.post(f"{APP_PREFIX}/settings")
@login_required
def save_settings():
    allowed = ("llm_endpoint", "llm_model", "llm_enabled", "schedule_enabled", "sender_name")
    with db() as conn:
        for key in allowed:
            value = request.form.get(key, "")
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    flash("设置已保存；密码和 API Key 仍只从服务器环境变量读取", "success")
    return redirect(url_for("dashboard"))


@app.post(f"{APP_PREFIX}/accounts")
@login_required
def add_account():
    email = request.form.get("email", "").strip().lower()
    secret = request.form.get("secret", "").strip()
    host = request.form.get("smtp_host", "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or not secret or not host:
        abort(400, "发件账号信息不完整")
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM sender_accounts").fetchone()[0]
        try:
            conn.execute("""INSERT INTO sender_accounts
                (label,email,smtp_host,smtp_port,use_ssl,secret_encrypted,enabled,is_default,created_at)
                VALUES(?,?,?,?,?,?,1,?,?)""",
                (request.form.get("label", "").strip() or email, email, host,
                 int(request.form.get("smtp_port") or 465), 1 if request.form.get("use_ssl") == "1" else 0,
                 encrypt_secret(secret), 1 if count == 0 else 0, iso(now())))
        except sqlite3.IntegrityError:
            flash("该发件邮箱已经存在", "error")
            return redirect(url_for("dashboard"))
    flash("发件账号已加密保存", "success")
    return redirect(url_for("dashboard"))


def subscriber_form_values():
    email = request.form.get("email", "").strip().lower()
    schedule_time = request.form.get("schedule_time", "19:00").strip()
    window_mode = request.form.get("window_mode", "days")
    try:
        window_value = max(1, min(90, int(request.form.get("window_value") or 1)))
    except ValueError:
        window_value = 1
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        abort(400, "收件邮箱格式无效")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule_time):
        abort(400, "推送时间格式无效")
    if window_mode not in ("hours", "days"):
        abort(400, "统计单位无效")
    source_ids = [int(value) for value in request.form.getlist("source_ids") if value.isdigit()]
    if not source_ids:
        abort(400, "请至少选择一个消息来源")
    return (request.form.get("name", "").strip() or email.split("@", 1)[0], email,
            schedule_time, window_mode, window_value, source_ids)


@app.post(f"{APP_PREFIX}/subscribers")
@login_required
def add_subscriber():
    name, email, schedule_time, window_mode, window_value, source_ids = subscriber_form_values()
    with db() as conn:
        try:
            cur = conn.execute("""INSERT INTO subscribers
                (name,email,schedule_time,window_mode,window_value,enabled,created_at)
                VALUES(?,?,?,?,?,1,?)""", (name, email, schedule_time, window_mode, window_value, iso(now())))
        except sqlite3.IntegrityError:
            flash("该收件邮箱已经存在", "error")
            return redirect(url_for("dashboard"))
        conn.executemany("INSERT INTO subscriber_sources(subscriber_id,source_id) VALUES(?,?)",
                         ((cur.lastrowid, source_id) for source_id in source_ids))
    flash("订阅用户已添加", "success")
    return redirect(url_for("dashboard"))


@app.post(f"{APP_PREFIX}/subscribers/<int:subscriber_id>/update")
@login_required
def update_subscriber(subscriber_id):
    name, email, schedule_time, window_mode, window_value, source_ids = subscriber_form_values()
    with db() as conn:
        try:
            conn.execute("""UPDATE subscribers SET name=?,email=?,schedule_time=?,window_mode=?,window_value=?
                WHERE id=?""", (name, email, schedule_time, window_mode, window_value, subscriber_id))
        except sqlite3.IntegrityError:
            flash("该收件邮箱已经存在", "error")
            return redirect(url_for("dashboard"))
        conn.execute("DELETE FROM subscriber_sources WHERE subscriber_id=?", (subscriber_id,))
        conn.executemany("INSERT INTO subscriber_sources(subscriber_id,source_id) VALUES(?,?)",
                         ((subscriber_id, source_id) for source_id in source_ids))
    flash("订阅设置已更新", "success")
    return redirect(url_for("dashboard"))


@app.post(f"{APP_PREFIX}/subscribers/<int:subscriber_id>/toggle")
@login_required
def toggle_subscriber(subscriber_id):
    with db() as conn:
        conn.execute("UPDATE subscribers SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (subscriber_id,))
    return redirect(url_for("dashboard"))


@app.post(f"{APP_PREFIX}/subscribers/<int:subscriber_id>/delete")
@login_required
def delete_subscriber(subscriber_id):
    with db() as conn:
        conn.execute("DELETE FROM subscribers WHERE id=?", (subscriber_id,))
    flash("订阅用户已删除", "success")
    return redirect(url_for("dashboard"))


@app.post(f"{APP_PREFIX}/accounts/<int:account_id>/default")
@login_required
def default_account(account_id):
    with db() as conn:
        conn.execute("UPDATE sender_accounts SET is_default=0")
        conn.execute("UPDATE sender_accounts SET is_default=1,enabled=1 WHERE id=?", (account_id,))
    flash("默认发件账号已更新", "success")
    return redirect(url_for("dashboard"))


@app.post(f"{APP_PREFIX}/accounts/<int:account_id>/delete")
@login_required
def delete_account(account_id):
    with db() as conn:
        target = conn.execute("SELECT is_default FROM sender_accounts WHERE id=?", (account_id,)).fetchone()
        conn.execute("DELETE FROM sender_accounts WHERE id=?", (account_id,))
        if target and target["is_default"]:
            conn.execute("UPDATE sender_accounts SET is_default=1 WHERE id=(SELECT id FROM sender_accounts ORDER BY id LIMIT 1)")
    flash("发件账号已删除", "success")
    return redirect(url_for("dashboard"))


@app.post(f"{APP_PREFIX}/run")
@login_required
def run_now():
    try:
        account_id = request.form.get("account_id", type=int)
        subscriber_id = request.form.get("subscriber_id", type=int)
        should_send = request.form.get("send") == "1"
        results = execute_digest(send=should_send, account_id=account_id, subscriber_id=subscriber_id)
        failed = [result for result in results if result["errors"]]
        run_ids = "、".join(f"#{result['run_id']}" for result in results)
        if failed:
            detail = "；".join(f"{result['subscriber']}：{'；'.join(result['errors'])}" for result in failed)
            if "Bad address syntax" in detail:
                detail = "收件邮箱格式错误，请使用逗号、分号或换行分隔多个地址"
            flash(("发送未完全成功" if should_send else "采集完成但有异常") + f"（任务 {run_ids}）：{detail}", "error")
        elif should_send:
            flash(f"发送成功（任务 {run_ids}），已分别投递给 {len(results)} 位订阅用户", "success")
        else:
            flash(f"采集成功（任务 {run_ids}），可在运行记录中预览", "success")
    except Exception as exc:
        flash(f"运行失败：{exc}", "error")
    return redirect(url_for("dashboard"))


@app.get(f"{APP_PREFIX}/runs/<int:run_id>/preview")
@login_required
def preview(run_id):
    with db() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run: abort(404)
        if run["subscriber_id"]:
            source_names = [row["name"] for row in conn.execute("""SELECT sources.name FROM sources
                JOIN subscriber_sources ON subscriber_sources.source_id=sources.id
                WHERE subscriber_sources.subscriber_id=? AND sources.enabled=1 ORDER BY sources.id""",
                (run["subscriber_id"],)).fetchall()]
            items = conn.execute("""SELECT items.*,sources.name source_name FROM items
                JOIN sources ON sources.id=items.source_id
                JOIN subscriber_sources ON subscriber_sources.source_id=sources.id
                WHERE subscriber_sources.subscriber_id=? AND published_at>=? AND published_at<?
                ORDER BY published_at DESC""", (run["subscriber_id"], run["window_start"], run["window_end"])).fetchall()
        else:
            source_names = None
            items = conn.execute("""SELECT items.*,sources.name source_name FROM items JOIN sources ON sources.id=items.source_id
                WHERE published_at>=? AND published_at<? ORDER BY published_at DESC""",
                (run["window_start"], run["window_end"])).fetchall()
    return render_email(parse_dt(run["window_start"]), parse_dt(run["window_end"]), items, "",
                        source_names=source_names)


@app.get('/digest/read/<token>')
def reader_page(token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        abort(404)
    with db() as conn:
        row = conn.execute('SELECT body FROM reader_pages WHERE token=? AND created_at>=?',
                           (token, iso(now() - timedelta(days=90)))).fetchone()
    if not row:
        abort(404)
    response = app.make_response(row['body'])
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


def scheduled_digest():
    with db() as conn:
        if get_settings(conn).get("schedule_enabled") != "1":
            return
        current = now()
        due = conn.execute("""SELECT id FROM subscribers WHERE enabled=1 AND schedule_time<=?
            AND (last_sent_at IS NULL OR substr(last_sent_at,1,10)<?) ORDER BY schedule_time,id""",
            (current.strftime("%H:%M"), current.strftime("%Y-%m-%d"))).fetchall()
    due_ids = [subscriber["id"] for subscriber in due]
    if due_ids:
        try:
            execute_digest(send=True, subscriber_ids=due_ids, scheduled=True)
        except Exception:
            app.logger.exception("订阅用户批量定时任务失败：%s", due_ids)


init_db()
scheduler = BackgroundScheduler(timezone=TZ, daemon=True)
scheduler.add_job(scheduled_digest, CronTrigger(minute="*", timezone=TZ),
                  id="daily_digest", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=1800)
scheduler.start()
