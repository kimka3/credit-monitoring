"""Read-only diagnostics of NICE public responses; no notification/state writes."""
import json
import re
import sys
import shutil
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import credit_monitor_daily as m


def probe(session, name, url, **kwargs):
    try:
        r = session.get(url, timeout=(15, 30), **kwargs)
        r.encoding = 'utf-8'
        title = re.search(r'<title[^>]*>(.*?)</title>', r.text, re.S | re.I)
        # Never log request headers, cookies or credentials. Body is only a short
        # public error-page excerpt, not a successful response's full contents.
        print(json.dumps(dict(name=name, status=r.status_code, url=r.url,
            server=r.headers.get('Server'), size=len(r.content),
            title=title[1].strip() if title else '',
            error=re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', r.text))[:1200] if r.status_code >= 400 else ''),
            ensure_ascii=False), flush=True)
    except m.requests.RequestException as exc:
        print(json.dumps(dict(name=name, error=str(exc)), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    with m.requests.Session() as s:
        s.headers.update(m.HEADERS)
        probe(s, 'fresh_watch', m.NICE_WATCH_URL)
        probe(s, 'main', 'https://www.nicerating.com/main.do')
        probe(s, 'after_main_watch', m.NICE_WATCH_URL)
        probe(s, 'changes', m.NICE_LIST_URL, params=dict(today=m.TODAY, cmpCd='',
              strDate=m.NICE_FROM, endDate=m.TODAY, ratingGubn='RO', searchType='0'))
    with m.requests.Session() as s:
        probe(s, 'default_headers_watch', m.NICE_WATCH_URL)
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if chrome:
        result = subprocess.run([chrome, '--headless', '--no-sandbox', '--disable-gpu',
            '--dump-dom', '--timeout=30000', m.NICE_WATCH_URL],
            capture_output=True, text=True, timeout=60)
        title = re.search(r'<title[^>]*>(.*?)</title>', result.stdout, re.S | re.I)
        print(json.dumps(dict(name='browser_watch', exit_code=result.returncode,
            title=title[1].strip() if title else '', size=len(result.stdout),
            has_watch_header='등급감시부호' in result.stdout), ensure_ascii=False), flush=True)
