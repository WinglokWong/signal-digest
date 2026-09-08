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
    body = app.render_email(app.now(), app.now(), items, '')
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
    assert reader_url.encode() in email_body.encode()
    assert ('href="' + reader_url + '#platform-0"').encode() in email_body.encode()
    print('READER PASS: anonymous token access, invalid token, absolute platform links, no-store', flush=True)

with app.db() as conn:
    source_count = conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
assert source_count == len(BUILTIN_SOURCES)
print(f'CATALOG PASS: {source_count} built-in sources seeded', flush=True)
check_reader()
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
