"""Replay today's server-collected disclosures through the normal NICE parsers."""
import base64
import gzip
import io
import json
from datetime import datetime, timedelta

from requests import Response


class DisclosureSession:
    def __init__(self, encoded, live, now, start, end, list_url, watch_url):
        if len(encoded) > 64000:
            raise ValueError('NICE input too large')
        with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(encoded, validate=True))) as stream:
            raw = stream.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError('NICE tables too large')
        data = json.loads(raw)
        age = now - datetime.fromisoformat(data['captured'])
        if not timedelta(0) <= age <= timedelta(hours=1) or (data['start'], data['end']) != (start, end):
            raise ValueError('NICE 원본 수집 시각 또는 조회 기간 불일치')
        self.pages = data['pages']
        if set(self.pages) != {'0', '6', '7', 'watch'} or not all(isinstance(x, str) and x for x in self.pages.values()):
            raise ValueError('NICE 원본 4종 누락')
        self.live = live
        self.list_url = list_url
        self.watch_url = watch_url

    def get(self, url, **kwargs):
        if url == self.list_url:
            kind = dict(kwargs['params'])['searchType']
        elif url == self.watch_url:
            kind = 'watch'
        else:
            return self.live.get(url, **kwargs)
        response = Response()
        response.status_code = 200
        response.url = url
        response._content = self.pages[kind].encode('utf-8')
        response.encoding = 'utf-8'
        return response
