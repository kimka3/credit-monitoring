import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import credit_monitor_daily as m
import monitor_checks as checks
from test_kis_parser import fetch as fetch_kis_html


KR_TABLES = ("data31", "data32", "data33", "data34", "data35", "data36",
             "data38", "data39", "data3A", "data3B", "data3C")

# 실제 사례: 한국기업평가 2025.12.24 엠에프엠코리아(주) 정지 -> D
SUSPENDED_TO_D = dict(COMP_NM='엠에프엠코리아(주)', EVAL_DT='2025.12.24', RBF_GRD_NM_ORG='정지',
                      CUR_GRD_NM_ORG='D', BFR_OL_NM=None, OL_NM=None, GR_CHN_DVCD='1',
                      EVAL_DIV_NM='수시', CRAL_NM=None)
NORMAL = dict(COMP_NM='정상회사', EVAL_DT='2025.12.24', RBF_GRD_NM_ORG='A', CUR_GRD_NM_ORG='A-',
              BFR_OL_NM='안정적', OL_NM='안정적', GR_CHN_DVCD='2', EVAL_DIV_NM='수시', CRAL_NM=None)


def fetch_kr(*items):
    blocks = {name: dict(Data=[]) for name in KR_TABLES}
    blocks['data31'] = dict(Data=list(items))

    class Session:
        def get(self, *a, **kw):
            return self
        post = get

        def raise_for_status(self):
            pass

        def json(self):
            return dict(data=blocks)

    return m.fetch_kr(Session())


def fetch_nice(prev, new):
    html = f'''<h2>채권 <strong>1건</strong></h2><table>
    <tr><th>기업명</th><th>회차</th><th>상환순위</th><th>종류</th><th>평정</th><th>직전등급</th>
    <th>직전전망</th><th>현재등급</th><th>현재전망</th><th>등급확정일</th></tr>
    <tr><td>나이스회사</td><td>1</td><td>선순위</td><td>SB</td><td>수시</td><td>{prev}</td>
    <td></td><td>{new}</td><td></td><td>2026.10.23</td></tr></table>'''

    class Session:
        text = html

        def get(self, *a, **kw):
            return self

        def raise_for_status(self):
            pass

    with patch.object(m, 'attach_nice_pdfs'):
        return m.fetch_nice(Session())


def kis_html(prev, new):
    return f'''<table><tr><th></th><th>회사명</th><th>평가종류</th><th>직전등급</th>
    <th>현재등급</th><th>평가일</th><th>리포트</th></tr>
    <tr><td></td><td>한신평회사</td><td>수시</td><td>{prev}</td><td>{new}</td>
    <td>2026.10.23</td><td>S</td></tr></table>'''


class SuspendedRatingTests(unittest.TestCase):
    def test_kr_suspended_to_default_does_not_stop_the_whole_source(self):
        rows = fetch_kr(NORMAL, SUSPENDED_TO_D)
        self.assertEqual({r['company'] for r in rows}, {'정상회사', '엠에프엠코리아(주)'})

    def test_kr_suspended_to_default_keeps_previous_status(self):
        row = next(r for r in fetch_kr(SUSPENDED_TO_D))
        self.assertEqual((row['prev_rating'], row['new_rating']), ('정지', 'D'))
        self.assertEqual(row['kind'], '등급변경')   # '신규평정'으로 보이면 안 된다
        self.assertEqual(m.describe_change(row), '정지 -> D')

    def test_detected_without_change_code(self):
        rows = fetch_kr(dict(SUSPENDED_TO_D, GR_CHN_DVCD='0'))
        self.assertEqual(len(rows), 1)

    def test_kr_rating_entering_suspension_is_a_change(self):
        rows = fetch_kr(dict(NORMAL, CUR_GRD_NM_ORG='정지', GR_CHN_DVCD='0'))
        self.assertEqual((rows[0]['prev_rating'], rows[0]['new_rating']), ('A', '정지'))
        self.assertEqual(rows[0]['kind'], '등급변경')

    def test_kis_has_no_change_code_and_still_detects_both_directions(self):
        for prev, new in (('정지', 'D'), ('BB', '정지')):
            rows = fetch_kis_html(kis_html(prev, new))
            self.assertEqual([(r['prev_rating'], r['new_rating']) for r in rows], [(prev, new)])
            self.assertEqual(rows[0]['kind'], '등급변경')

    def test_nice_has_no_change_code_and_still_detects_both_directions(self):
        for prev, new in (('정지', 'D'), ('BB', '정지')):
            rows = fetch_nice(prev, new)
            self.assertEqual([(r['prev_rating'], r['new_rating']) for r in rows], [(prev, new)])
            self.assertEqual(rows[0]['kind'], '등급변경')

    def test_unchanged_suspension_is_not_reported(self):
        self.assertEqual(fetch_kis_html(kis_html('정지', '정지')), [])

    def test_entering_suspension_is_flagged_in_the_message(self):
        row = fetch_kr(dict(NORMAL, CUR_GRD_NM_ORG='정지'))[0]
        self.assertIn('⚠️ [등급변경]', m.format_plain([row]))
        self.assertIn('A -> 정지', m.format_plain([row]))

    def test_unknown_status_still_fails_loudly(self):
        with self.assertRaises(checks.IntegrityError):
            fetch_kr(dict(SUSPENDED_TO_D, RBF_GRD_NM_ORG='보류'))
        with self.assertRaises(checks.IntegrityError):
            fetch_kr(dict(NORMAL, CUR_GRD_NM_ORG='보류'))

    def test_withdrawn_rating_is_still_excluded(self):
        self.assertEqual(fetch_kr(dict(NORMAL, CUR_GRD_NM_ORG='취소')), [])

    def test_suspension_has_no_rank_and_is_not_called_up_or_down(self):
        self.assertIsNone(m.rating_rank('정지'))
        self.assertEqual(m.change_kind(dict(prev_rating='정지', new_rating='D')), '등급변경')

    def test_end_to_end_alert_history_and_report(self):
        changes = fetch_kr(SUSPENDED_TO_D)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            report = root / 'report.html'
            original_write = m.write_html
            with patch.object(m, 'STATE_PATH', root / 'sent.json'), \
                 patch.object(m, 'HISTORY_PATH', root / 'history.json'), \
                 patch.object(m, 'HEALTH_PATH', root / 'health.json'), \
                 patch.object(m, 'scrape_all', return_value=(changes, [], None)), \
                 patch.object(m, 'ANTHROPIC_API_KEY', ''), \
                 patch.object(m, 'send_tg') as sender, \
                 patch.object(m, 'send_reports'), \
                 patch.object(m, 'backfill_pdf_links'), \
                 patch.object(m, 'write_html', side_effect=lambda h, e: original_write(h, e, report)):
                self.assertEqual(m.main(), 0)
                self.assertIn('⚠️ [등급변경] 엠에프엠코리아(주) | 정지 -> D', sender.call_args.args[0])
                history = json.loads((root / 'history.json').read_text(encoding='utf-8'))
                self.assertEqual(history[m.item_key(changes[0])]['prev_rating'], '정지')
                self.assertIn('엠에프엠코리아', report.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
