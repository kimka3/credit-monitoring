import base64
import gzip
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import credit_monitor_daily as m
from nice_feed import DisclosureSession
from ops.vultr import nice_feed as producer
from test_watch import NICE


class ServerFeedTests(unittest.TestCase):
    def test_server_dispatch_roundtrip(self):
        now = datetime.now(producer.KST)
        ordinary = ('<h2>채권 <strong>0건</strong></h2><table><tr><th>기업명</th>'
                    '<th>등급확정일</th></tr><tr><td>등록된 정보가 없습니다.</td></tr></table>')

        def read(request, timeout):
            from io import BytesIO
            html = NICE if 'WatchList' in request.full_url else ordinary
            stream = BytesIO(html.encode())
            stream.geturl = lambda: request.full_url
            return stream

        with patch.object(producer, 'urlopen', side_effect=read):
            bundle = producer.collect()
        end = now.date().isoformat()
        start = (now.date() - timedelta(days=30)).isoformat()
        live = Mock()
        def session(value, when):
            return DisclosureSession(value, live, when, start, end, m.NICE_LIST_URL, m.NICE_WATCH_URL)
        current = datetime.now(producer.KST)
        server = session(bundle, current)
        for kind in ('0', '6', '7'):
            self.assertEqual(m.fetch_nice(server, kind), [])
        with patch.multiple(m, WATCH_FROM='2026.09.28', TODAY='2026-10-28'):
            self.assertEqual(len(m.fetch_nice_watch(server)), 2)
        live.get.assert_not_called()
        with self.assertRaises(ValueError):
            session(bundle, current + timedelta(hours=2))
        data = json.loads(gzip.decompress(base64.b64decode(bundle)))
        del data['pages']['7']
        incomplete = base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()
        with self.assertRaises(ValueError):
            session(incomplete, current)
