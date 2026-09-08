"""Public, first-party news lists. No scripts or remote code are executed."""
import hashlib
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

CATALOG = [
    ('Anthropic 官方新闻', 'web', 'https://www.anthropic.com/news'),
    ('Google DeepMind 官方动态', 'rss', 'https://deepmind.google/blog/rss.xml'),
    ('Qwen 官方动态', 'web', 'https://qwen.ai/research'),
    ('NVIDIA 官方动态', 'rss', 'https://blogs.nvidia.com/feed/'),
    ('中国人民银行 新闻', 'web', 'https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html'),
    ('证监会要闻', 'web', 'https://www.csrc.gov.cn/'),
    ('国家统计局 数据发布', 'web', 'https://www.stats.gov.cn/sj/zxfb/'),
    ('美联储 官方发布', 'rss', 'https://www.federalreserve.gov/feeds/press_all.xml'),
]

BUILTIN_SOURCES = [
    ('新智元', 'web', 'https://aiera.com.cn/'),
    ('财联社 A股资讯', 'web', 'https://www.cls.cn/depth?id=1003'),
    ('新浪财经 要闻', 'web', 'https://finance.sina.com.cn/'),
    ('金十数据 热点头条', 'web', 'https://xnews.jin10.com/53'),
] + CATALOG


def get_with_retry(url, *, user_agent, params=None):
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=(8, 20),
                                    headers={'User-Agent': user_agent})
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
    raise last_error

def fetch_official(source, parse_dt, clean_text, user_agent):
    url = source['url']
    host = urlparse(url).hostname
    if host == 'qwen.ai':
        response = get_with_retry('https://qwen.ai/api/v2/article/retrieval', user_agent=user_agent,
                                  params={'type': 'qwen_ai', 'language': 'en-US'})
        articles = response.json().get('data', {}).get('articles', [])
        results = []
        for article in articles:
            extra = article.get('extra') or {}
            published = parse_dt(extra.get('date'))
            path = article.get('path')
            if not published or not path:
                continue
            target = 'https://qwen.ai/blog?id=' + path
            results.append(dict(guid='qwen-' + str(article.get('id') or path),
                                title=clean_text(article.get('title'), 300), url=target,
                                summary=clean_text(extra.get('introduction') or extra.get('description') or ''),
                                published=published))
        if not results:
            raise ValueError('Qwen 官方接口未返回可识别的文章')
        return results
    response = get_with_retry(url, user_agent=user_agent)
    soup = BeautifulSoup(response.content, 'html.parser')
    results, seen = [], set()
    for link in soup.select('a[href]'):
        target = urljoin(url, link['href'])
        if urlparse(target).hostname != host or target in seen:
            continue
        path = urlparse(target).path
        if host == 'www.anthropic.com':
            valid = path.startswith('/news/')
        elif host == 'www.csrc.gov.cn':
            valid = '/c100028/' in path and (path.endswith('.shtml') or path.endswith('/content.shtml')) and 'common_' not in path
        elif host == 'www.stats.gov.cn':
            valid = '/sj/zxfb/' in path and path.endswith('.html')
        elif host == 'www.pbc.gov.cn':
            valid = '/113469/' in path and target != url and path.endswith('.html')
        else:
            raise ValueError('该官方来源尚未完成公开列表适配')
        if not valid:
            continue
        if host == 'www.csrc.gov.cn':
            try:
                detail = get_with_retry(target, user_agent=user_agent)
                detail_soup = BeautifulSoup(detail.content, 'html.parser')
                date_meta = detail_soup.select_one('meta[name="createDate"],meta[name="PubDate"]')
                published = parse_dt(date_meta.get('content')) if date_meta else None
            except requests.RequestException:
                published = None
        title = link.get('title') or link.get_text(' ', strip=True)
        node = link
        published = published if host == 'www.csrc.gov.cn' else None
        for _ in range(4):
            time_tag = node.select_one('time')
            if time_tag:
                published = parse_dt(time_tag.get('datetime') or time_tag.get_text(' ', strip=True))
            text = node.get_text(' ', strip=True)
            match = re.search(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}|[A-Z][a-z]{2,8}\s+\d{1,2},\s*\d{4}', text)
            if not published and match:
                published = parse_dt(match.group())
            if published:
                break
            if not node.parent or node.parent.name in ('body', 'html'):
                break
            node = node.parent
        if not published or not title:
            continue
        seen.add(target)
        title_node = link.select_one('h2,h3,h4,[class*="title"], [class*="Title"]')
        if title_node:
            title = title_node.get_text(' ', strip=True)
        results.append(dict(guid=hashlib.sha256(target.encode()).hexdigest(),
                            title=clean_text(title, 300), url=target, summary='', published=published))
    if not results:
        raise ValueError('官方列表未返回可识别的文章和日期，需要检查页面适配')
    return results
