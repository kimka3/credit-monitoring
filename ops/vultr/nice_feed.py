"""Collect the four public NICE tables on the timer host, using only stdlib."""
import base64
import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = 'https://www.nicerating.com/disclosure/'
KST = timezone(timedelta(hours=9))
KINDS = ('0', '6', '7', 'watch')


def collect():
    now = datetime.now(KST)
    today = now.date().isoformat()
    start = (now.date() - timedelta(days=30)).isoformat()
    pages = {}
    for kind in KINDS:
        url = ROOT + 'totalWatchList.do' if kind == 'watch' else (
            ROOT + 'ratingChangeList.do?' + urlencode(dict(today=today, cmpCd='',
                strDate=start, endDate=today, ratingGubn='RO', searchType=kind)))
        request = Request(url, headers={'User-Agent': 'CreditMonitor/1.0', 'Accept-Language': 'ko-KR'})
        with urlopen(request, timeout=20) as response:
            if response.geturl().split('?')[0] != url.split('?')[0]:
                raise ValueError('NICE redirected outside the requested disclosure page')
            html = response.read(2_000_001)
        if len(html) > 2_000_000:
            raise ValueError('NICE response exceeds input limit')
        # Transport public count headings and table rows only.
        parts = [
            match[0] for match in re.finditer(r'<(h2|table)\b[^>]*>.*?</\1>', html.decode('utf-8'), re.I | re.S)]
        page = '\n'.join(parts)
        if not ('등록일' in page and '등급감시부호' in page if kind == 'watch' else '기업명' in page and '확정일' in page):
            raise ValueError(f'NICE {kind}: missing disclosure headers')
        pages[kind] = page
    payload = dict(captured=now.isoformat(), start=start, end=today, pages=pages)
    encoded = base64.b64encode(gzip.compress(json.dumps(payload, ensure_ascii=False).encode())).decode()
    if len(encoded) > 64000:
        raise ValueError('NICE bundle exceeds workflow input limit')
    return encoded


if __name__ == '__main__':
    print(collect())
