import json
import os
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import credit_monitor_daily as m
from ops.pc import nice_dispatch as pc
from ops.vultr import nice_feed

THURSDAY = datetime(2026, 11, 19, 8, 15, tzinfo=nice_feed.KST)
SATURDAY = datetime(2026, 11, 21, 8, 15, tzinfo=nice_feed.KST)


def response(status=200, text=''):
    r = Mock()
    r.status_code = status
    r.text = text
    r.raise_for_status = Mock()
    return r


class PcDispatchTests(unittest.TestCase):
    def run_main(self, now=THURSDAY, health=None, post_status=204, argv=()):
        session = Mock()
        session.get.return_value = response(text=json.dumps(health or {'last_success': '2026-11-18'}))
        session.post.return_value = response(post_status)
        with patch.object(pc, 'read_token', return_value='github_pat_TEST'), \
                patch.object(pc, 'collect', return_value='QUJD') as collect, \
                patch.object(pc.requests, 'Session') as factory, \
                patch.object(pc, 'log'):
            factory.return_value.__enter__.return_value = session
            code = pc.main(list(argv), now=now)
        return code, session, collect

    def test_dispatches_with_server_side_nice_tables(self):
        code, session, collect = self.run_main()
        self.assertEqual(code, 0)
        collect.assert_called_once()
        url = session.post.call_args.args[0]
        self.assertTrue(url.endswith('/actions/workflows/daily.yml/dispatches'))
        self.assertEqual(session.post.call_args.kwargs['json'],
                         {'ref': 'main', 'inputs': {'nice_disclosures': 'QUJD'}})
        self.assertEqual(session.post.call_args.kwargs['headers']['Authorization'], 'Bearer github_pat_TEST')

    def test_skips_weekends_without_network(self):
        code, session, collect = self.run_main(now=SATURDAY)
        self.assertEqual(code, 0)
        collect.assert_not_called()
        session.post.assert_not_called()

    def test_skips_when_today_already_completed(self):
        code, session, collect = self.run_main(health={'last_success': '2026-11-19', 'active_issues': {}})
        self.assertEqual(code, 0)
        collect.assert_not_called()
        session.post.assert_not_called()

    def test_force_runs_even_if_completed(self):
        code, session, collect = self.run_main(health={'last_success': '2026-11-19', 'active_issues': {}},
                                               argv=['--force'])
        self.assertEqual(code, 0)
        session.post.assert_called_once()

    def test_reports_dispatch_failure(self):
        code, _, _ = self.run_main(post_status=403)
        self.assertEqual(code, 1)

    def test_missing_token_fails_before_collecting(self):
        with patch.object(pc, 'read_token', side_effect=OSError('자격 증명 없음')), \
                patch.object(pc, 'collect') as collect, patch.object(pc, 'log'):
            self.assertEqual(pc.main([], now=THURSDAY), 1)
        collect.assert_not_called()


class ActionsSkipTests(unittest.TestCase):
    def run_monitor(self, env, health):
        with patch.dict(os.environ, env, clear=False), \
                patch.object(m, 'latest_health', return_value=health), \
                patch.object(m, 'main', return_value='ran') as scrape:
            return m.cli(), scrape

    def test_tests_inside_actions_are_not_skipped(self):
        # 회귀 테스트는 Actions 안에서 main()/run_monitor()를 직접 부른다.
        # 오늘 정상 완료 기록이 있어도 건너뛰면 안 된다 (2026-11-26 08:27 실패).
        sentinel = RuntimeError('scrape started')
        with patch.dict(os.environ, self.base_env(), clear=False), \
                patch.object(m, 'latest_health',
                             return_value={'last_success': m.TODAY, 'active_issues': {}}) as health, \
                patch.object(m, 'scrape_all', side_effect=sentinel):
            with self.assertRaises(RuntimeError) as ctx:
                m.run_monitor()
        self.assertIs(ctx.exception, sentinel)
        health.assert_not_called()

    def base_env(self, **extra):
        env = {'GITHUB_ACTIONS': 'true', 'NICE_DISCLOSURES': '', 'FORCE_RUN': 'false'}
        env.update(extra)
        return env

    def test_backup_trigger_skips_after_successful_run_today(self):
        result, scrape = self.run_monitor(self.base_env(), {'last_success': m.TODAY, 'active_issues': {}})
        self.assertEqual(result, 0)
        scrape.assert_not_called()

    def test_backup_trigger_runs_if_today_not_completed(self):
        result, _ = self.run_monitor(self.base_env(), {'last_success': '2026-01-01', 'active_issues': {}})
        self.assertEqual(result, 'ran')

    def test_backup_trigger_runs_if_today_had_warnings(self):
        result, _ = self.run_monitor(self.base_env(), {'last_success': m.TODAY, 'active_issues': {'k': 'x'}})
        self.assertEqual(result, 'ran')

    def test_run_with_nice_tables_is_never_skipped(self):
        result, _ = self.run_monitor(self.base_env(NICE_DISCLOSURES='QUJD'), {'last_success': m.TODAY, 'active_issues': {}})
        self.assertEqual(result, 'ran')

    def test_manual_force_is_never_skipped(self):
        result, _ = self.run_monitor(self.base_env(FORCE_RUN='true'), {'last_success': m.TODAY, 'active_issues': {}})
        self.assertEqual(result, 'ran')


if __name__ == '__main__':
    unittest.main()
