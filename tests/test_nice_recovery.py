import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import credit_monitor_daily as m
from monitor_checks import IntegrityError, write_json
from ops.recover_nice_watch import recover, snapshot_rows
from test_watch import NICE


def snapshot(now):
    return dict(source_url=m.NICE_WATCH_URL, captured_at=now.isoformat(),
                html=NICE, sha256=hashlib.sha256(NICE.encode('utf-8')).hexdigest())


class NiceRecoveryTests(unittest.TestCase):
    def test_stale_future_tampered_and_unofficial_snapshots_rejected(self):
        now = datetime.now(timezone.utc)
        cases = [snapshot(now - timedelta(hours=25)), snapshot(now + timedelta(minutes=5)),
                 dict(snapshot(now), html=NICE + 'changed'),
                 dict(snapshot(now), source_url='https://example.com')]
        for case in cases:
            with self.subTest(case=case['captured_at']), self.assertRaises(IntegrityError):
                snapshot_rows(case, now)

    def test_backfill_sends_once_and_preserves_live_failure(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            report = root / 'report.html'
            health = dict(active_issues={'a': 'NICE Watch: HTTP 400'}, last_success='2026-10-27')
            write_json(root / 'health.json', health)
            old_health = (root / 'health.json').read_bytes()
            original_write = m.write_html
            with patch.multiple(m, STATE_PATH=root/'sent.json', HISTORY_PATH=root/'history.json',
                                HEALTH_PATH=root/'health.json', WATCH_FROM='2026.09.28', TODAY='2026-10-28'), \
                 patch.object(m, 'send_tg') as sender, \
                 patch.object(m, 'github_output') as outputs, \
                 patch.object(m, 'write_html', side_effect=lambda h,e:original_write(h,e,report)):
                data = snapshot(datetime.now(timezone.utc))
                self.assertEqual(recover(data), 1)
                self.assertIn('자동 수집 접속 오류는 아직 해결되지 않았습니다', sender.call_args.args[0])
                self.assertEqual(recover(data), 1)
                self.assertEqual(sender.call_count, 1)
                self.assertEqual(len(json.loads((root/'sent.json').read_text(encoding='utf-8'))), 2)
                self.assertEqual(len(json.loads((root/'history.json').read_text(encoding='utf-8'))), 2)
                self.assertIn('HTTP 400', report.read_text(encoding='utf-8'))
                self.assertEqual((root/'health.json').read_bytes(), old_health)
                outputs.assert_any_call('report_ready', 'true')

    def test_delivery_failure_leaves_sent_history_unmodified(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            write_json(root/'health.json', {'active_issues': {'a': 'HTTP 400'}})
            with patch.multiple(m, STATE_PATH=root/'sent.json', HISTORY_PATH=root/'history.json',
                                HEALTH_PATH=root/'health.json', WATCH_FROM='2026.09.28', TODAY='2026-10-28'), \
                 patch.object(m, 'send_tg', side_effect=RuntimeError('unavailable')):
                with self.assertRaises(RuntimeError):
                    recover(snapshot(datetime.now(timezone.utc)))
                self.assertFalse((root/'sent.json').exists())
                self.assertFalse((root/'history.json').exists())
