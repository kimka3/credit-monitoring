import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import credit_monitor_daily as monitor


# Issuer Rating 헤더는 직전/현재 각각 colspan=2이고 데이터는 12열이다.
ISSUER = """<table>
<tr><th></th><th>회사명</th><th>재무기준일</th><th>평가종류</th>
<th colspan="2">직전</th><th></th><th colspan="2">현재</th>
<th>기준통화</th><th>평가일</th><th>리포트</th></tr>
<tr><th>등급</th><th>Outlook</th><th></th><th>등급</th><th>Outlook</th></tr>
<tr><td></td><td>우리투자증권</td><td>2025.12.31</td><td>수시</td>
<td>A+</td><td>안정적</td><td></td><td>A+</td><td>긍정적</td>
<td>원화/외화</td><td>2026.10.23</td><td>
<a href="javascript:fn_file('R8', '2', '우리투자증권', 'rs20261026-13.pdf','Rating Summary','20261023')">S</a></td></tr>
<tr><td></td><td>하나은행</td><td>2025.12.31</td><td>본</td>
<td>AAA</td><td>안정적</td><td></td><td>AAA</td><td>안정적</td>
<td>원화/외화</td><td>2026.10.23</td><td>S</td></tr></table>"""

BOND = """<table><tr><th></th><th>회사명</th><th>채권종류</th>
<th>평가종류</th><th>직전등급</th><th></th><th>현재등급</th>
<th>평가일</th><th>리포트</th></tr>
<tr><th>Outlook</th><th></th><th>Outlook</th></tr>
<tr><td></td><td>채권회사</td><td>SB</td><td>수시</td><td>BBB+</td>
<td></td><td>BBB+</td><td>2026.10.23</td><td>S</td></tr>
<tr><td>안정적</td><td></td><td>부정적</td></tr></table>"""


def fetch(html):
    class Response:
        text = html

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    return monitor.fetch_kis(Session())


class KisParserTests(unittest.TestCase):
    def test_issuer_outlook_and_matching_pdf(self):
        rows = fetch(ISSUER)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['company'], '우리투자증권')
        self.assertEqual((row['prev_rating'], row['new_rating']), ('A+', 'A+'))
        self.assertEqual((row['prev_outlook'], row['new_outlook']), ('안정적', '긍정적'))
        self.assertEqual((row['kind'], row['date']), ('전망변경', '2026.10.23'))
        self.assertEqual(row['pdfs'][0]['file'], 'rs20261026-13.pdf')

    def test_bond_outlook_on_following_row(self):
        rows = fetch(BOND)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], '전망변경')
        self.assertEqual(rows[0]['new_outlook'], '부정적')

    def test_insurance_uses_same_grouped_header(self):
        rows = fetch(ISSUER.replace('우리투자증권', '보험회사'))
        self.assertEqual(rows[0]['company'], '보험회사')
        self.assertEqual(rows[0]['kind'], '전망변경')

    def test_pdf_is_from_changed_row_not_other_security(self):
        cp = '''<table><tr><th></th><th>회사명</th><th>평가종류</th><th>직전등급</th>
        <th>현재등급</th><th>평가일</th><th>리포트</th></tr>
        <tr><td></td><td>우리투자증권</td><td>정기</td><td>A1</td><td>A1</td>
        <td>2026.10.23</td><td><a href="javascript:fn_file('R8', '2', '우리투자증권',
        'rs20261026-14.pdf','Rating Summary','20261023')">S</a></td></tr></table>'''
        rows = fetch(cp + ISSUER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['pdfs'][0]['file'], 'rs20261026-13.pdf')

    def test_separate_table_headers_do_not_leak(self):
        rows = fetch(BOND + ISSUER)
        self.assertEqual([r['company'] for r in rows], ['채권회사', '우리투자증권'])

    def test_shifted_date_is_an_error(self):
        with self.assertRaises(monitor.StructureError):
            fetch(ISSUER.replace('<td>2026.10.23</td>', '<td>extra</td><td>2026.10.23</td>'))

    def test_missing_table_is_an_error(self):
        with self.assertRaises(monitor.StructureError):
            fetch('<html>blocked</html>')

    def test_nice_parser_keeps_existing_colspan_behavior(self):
        parser = monitor.TableParser()
        parser.feed('<tr><td colspan="10">등록된 정보가 없습니다.</td></tr>')
        self.assertEqual(parser.rows, [['등록된 정보가 없습니다.']])

    def test_send_once_and_write_report_after_success(self):
        changes = fetch(ISSUER)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            report = root / 'report.html'
            original_write = monitor.write_html
            with patch.object(monitor, 'STATE_PATH', root / 'sent.json'), \
                 patch.object(monitor, 'HISTORY_PATH', root / 'history.json'), \
                 patch.object(monitor, 'scrape_all', return_value=(changes, [], None)), \
                 patch.object(monitor, 'ANTHROPIC_API_KEY', ''), \
                 patch.object(monitor, 'send_tg') as sender, \
                 patch.object(monitor, 'send_reports'), \
                 patch.object(monitor, 'backfill_pdf_links'), \
                 patch.object(monitor, 'write_html', side_effect=lambda h, e: original_write(h, e, report)):
                self.assertEqual(monitor.main(), 0)
                self.assertIn('전망 안정적 -> 긍정적', sender.call_args.args[0])
                state = json.loads((root / 'sent.json').read_text(encoding='utf-8'))
                self.assertIn(monitor.item_key(changes[0]), state)
                self.assertIn('우리투자증권', report.read_text(encoding='utf-8'))
                history = json.loads((root / 'history.json').read_text(encoding='utf-8'))
                self.assertEqual(history[monitor.item_key(changes[0])]['kind'], '전망변경')
                self.assertEqual(monitor.main(), 0)
                self.assertIn('신규 등급변동 내역이 없습니다', sender.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
