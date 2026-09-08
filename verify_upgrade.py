"""Run in an isolated temporary DB; no mail or subscriber updates."""
import os
import tempfile
os.environ['DATABASE_PATH'] = tempfile.mkdtemp(prefix='digest-check-') + '/check.db'
os.environ.setdefault('PUBLIC_BASE_URL', 'https://digest.example.com')
import app
app.scheduler.shutdown(wait=False)
from official_sources import BUILTIN_SOURCES, CATALOG, fetch_official
from unittest.mock import patch
from bs4 import BeautifulSoup

def check_reader():
    items = [dict(source_name='Platform A', title='Headline', summary='Summary', url='https://example.com/article', published_at=app.iso(app.now()))]
    body = app.render_email(app.now(), app.now(), items, '', source_names=['Platform A', 'Platform Empty'])
    with app.db() as conn:
        reader_url, email_body = app.publish_reader_page(conn, body)
    token = reader_url.rsplit('/', 1)[1]
    client = app.app.test_client()
    response = client.get('/digest/read/' + token)
    assert response.status_code == 200 and b'Platform A' in response.data
    assert client.get('/digest/read/' + 'x'*43).status_code == 404
    assert response.headers['Cache-Control'] == 'private, no-store'
    soup = BeautifulSoup(body, 'html.parser')
    assert all(soup.find(id=a['href'][1:]) for a in soup.select('a[href^="#"]'))
    assert 'Platform Empty' not in body and '今日无更新' not in body
    assert reader_url.encode() in email_body.encode()
    assert ('href="' + reader_url + '#platform-0"').encode() in email_body.encode()
    print('READER PASS: anonymous token access, invalid token, absolute platform links, no-store', flush=True)

def check_batched_collection():
    with app.db() as conn:
        source_id = conn.execute('SELECT id FROM sources ORDER BY id LIMIT 1').fetchone()[0]
        subscriber_ids = []
        for number in (1, 2):
            cur = conn.execute('''INSERT INTO subscribers
                (name,email,schedule_time,window_mode,window_value,enabled,created_at)
                VALUES(?,?,?,?,?,1,?)''', (f'Test {number}', f'test{number}@example.com', '19:00',
                'days', 1, app.iso(app.now())))
            subscriber_ids.append(cur.lastrowid)
            conn.execute('INSERT INTO subscriber_sources VALUES(?,?)', (cur.lastrowid, source_id))
    calls = []
    def fake_collect(conn, source, start, end):
        calls.append(source['id'])
        return 0
    with patch.object(app, 'collect_source', side_effect=fake_collect):
        results = app.execute_digest(send=False, subscriber_ids=subscriber_ids)
    assert len(results) == 2 and len(calls) == 1
    print('BATCH PASS: two subscribers share one source collection', flush=True)

with app.db() as conn:
    source_count = conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
assert source_count == len(BUILTIN_SOURCES)
print(f'CATALOG PASS: {source_count} built-in sources seeded', flush=True)
check_reader()
check_batched_collection()
for name, kind, url in CATALOG:
    try:
        source = dict(name=name,kind=kind,url=url)
        rows = app.fetch_rss(source) if kind == 'rss' else fetch_official(source, app.parse_dt, app.clean_text, app.USER_AGENT)
        if not rows:
            raise ValueError('empty')
        newest = max(row['published'] for row in rows)
        print(name, len(rows), newest.isoformat(), rows[0]['title'][:70], flush=True)
    except Exception as exc:
        print(name, 'FAILED', str(exc)[:200], flush=True)

custom_sources = [
    ('新智元', app.fetch_aiera, {'url': 'https://aiera.com.cn/'}),
    ('财联社 A股资讯', app.fetch_cls, {'url': 'https://www.cls.cn/depth?id=1003'}),
    ('新浪财经 要闻', app.fetch_sina, {'url': 'https://finance.sina.com.cn/'}),
    ('金十数据 热点头条', app.fetch_jin10, {'url': 'https://xnews.jin10.com/53'}),
]
for name, fetcher, source in custom_sources:
    try:
        rows = fetcher(source)
        if not rows:
            raise ValueError('empty')
        newest = max(row['published'] for row in rows)
        print(name, len(rows), newest.isoformat(), rows[0]['title'][:70], flush=True)
    except Exception as exc:
        print(name, 'FAILED', str(exc)[:200], flush=True)
