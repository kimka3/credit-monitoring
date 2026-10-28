import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import credit_monitor_daily as m
import monitor_checks as checks
from test_kis_parser import ISSUER, fetch


ROW = dict(source='한국신용평가', company='우리투자증권', date='2026.10.23',
           prev_rating='A+', new_rating='A+', prev_outlook='안정적',
           new_outlook='긍정적', kind='전망변경', eval_type='수시')


class SourceIntegrityTests(unittest.TestCase):
    def test_mixed_valid_and_invalid_nice_rows_are_rejected(self):
        html='''<h2>채권 <strong>2건</strong></h2><table>
        <tr><th>기업명</th><th>등급확정일</th></tr>
        <tr><td>정상회사</td><td>수시</td><td>A</td><td>Stable</td><td>A</td><td>Positive</td><td>2026.10.23</td></tr>
        <tr><td>누락회사</td><td>A</td><td>A+</td><td>extra</td><td>2026.10.23</td><td>extra</td></tr></table>'''
        class Session:
            text=html
            def get(self,*a,**kw): return self
            def raise_for_status(self): pass
        with self.assertRaises(checks.IntegrityError):
            m.fetch_nice(Session())

    def test_nice_sf_uses_security_grade_columns(self):
        html='''<h2>자산유동화 증권 <strong>1건</strong></h2><table>
        <tr><th>기업명</th><th>회차</th><th>종류</th><th>평정</th><th>직전등급</th><th>현재등급</th><th>등급확정일</th></tr>
        <tr><td>유동화회사</td><td>1</td><td>ABCP</td><td>수시</td><td>A2 (sf)</td><td>A2- (sf)</td><td>2026.10.23</td></tr></table>'''
        class Session:
            text=html
            def get(self,*a,**kw): return self
            def raise_for_status(self): pass
        with patch.object(m,'attach_nice_pdfs'):
            rows=m.fetch_nice_sf(Session())
        self.assertEqual(rows[0]['kind'],'등급하향')
        self.assertIn('|instrument:SF',m.item_key(rows[0]))

    def test_filter_drop_is_detected_independently(self):
        with patch.object(m,'filter_changes',return_value=[]), self.assertRaises(checks.IntegrityError):
            m.checked_changes([dict(ROW)],'한국신용평가')

    def test_issuer_row_drop_disagrees_with_original_count(self):
        with self.assertRaises(checks.IntegrityError):
            fetch(ISSUER.replace('<td>2026.10.23</td>','<td>2026/10/23</td>',1))

    def test_unknown_grade_is_not_silently_skipped(self):
        with self.assertRaises(checks.IntegrityError):
            fetch(ISSUER.replace('<td>A+</td><td>긍정적</td>','<td>NEW-GRADE</td><td>긍정적</td>'))

    def test_kis_withdrawal_is_accounted_without_dropping_other_changes(self):
        from test_kis_parser import BOND
        withdrawn = BOND.replace('<td>BBB+</td><td>2026.10.23</td>',
                                 '<td>WR</td><td>2026.10.23</td>')
        rows = fetch(withdrawn + ISSUER)
        self.assertEqual([r['company'] for r in rows], ['우리투자증권'])
        self.assertEqual(m.SOURCE_STATS['한국신용평가']['raw'], 3)
        self.assertEqual(m.SOURCE_STATS['한국신용평가']['excluded_cancel'], 1)
        self.assertEqual(fetch(withdrawn), [])
        self.assertEqual(m.valid_previous('WR', 'KIS'), '')

    def test_kis_original_count_mismatch(self):
        with self.assertRaises(checks.IntegrityError):
            checks.validate_kis_count('<h2>Issuer <em>2</em></h2>',1)

    def test_nice_original_count_mismatch(self):
        with self.assertRaises(checks.IntegrityError):
            checks.validate_html_count('<h2>채권 <strong>2건</strong></h2>',1,'NICE')

    def test_valid_explicit_zero(self):
        checks.validate_html_count('<h2>채권 <strong>0건</strong></h2>등록된 정보가 없습니다.',0,'NICE')

    def test_no_count_is_not_a_verified_zero(self):
        with self.assertRaises(checks.IntegrityError):
            checks.validate_html_count('<table><tr><th>기업명</th></tr></table>',0,'NICE')

    def test_api_structure_drift_is_not_an_empty_list(self):
        for data in [{}, {'data1':{'Data':{}}}, {'data1':{'Data':[None]}}]:
            with self.subTest(data=data), self.assertRaises(checks.IntegrityError):
                checks.json_tables(data,'KR')

    def test_one_missing_product_table_is_detected_even_with_other_rows(self):
        data = {'data1': {'Data': [{'company': '정상회사'}]}, 'data2': {}}
        with self.assertRaises(checks.IntegrityError):
            checks.json_tables(data, 'KR Watch', ('data1', 'data2'))

    def test_nice_watch_row_with_missing_date_is_not_silently_dropped(self):
        from test_watch import NICE, Session
        with self.assertRaises(checks.IntegrityError):
            m.fetch_nice_watch(Session(NICE.replace('2026.10.01', '')))

    def test_volume_warning_keeps_baseline_until_recovery(self):
        old={'sources':{'KR':{'parsed':100,'latest_date':'2026.10.27'}}}
        small={'KR':{'parsed':2,'latest_date':'2026.10.27'}}
        issues=checks.volume_issues(old,small,'2026.09.28')
        self.assertEqual(len(issues),1)
        checks.update_health(old,issues,'2026-10-28',small)
        self.assertEqual(old['sources']['KR']['parsed'],100)
        self.assertEqual(checks.pending_alerts(old,issues),[])
        self.assertEqual(len(checks.volume_issues(old,small,'2026.09.28')),1)

    def test_small_watch_list_unexpectedly_zero_is_suspicious(self):
        health={'sources':{'NICE Watch':{'parsed':5,'latest_date':'2026.10.13'}}}
        self.assertEqual(len(checks.volume_issues(health,{'NICE Watch':{'parsed':0}},'2026.09.28')),1)
        self.assertEqual(checks.volume_issues(health,{'NICE Watch':{'parsed':0}},'2026.10.14'),[])


class RuntimeIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.stack=[]
        def start(p):
            self.stack.append(p)
            return p.start()
        self.start=start
        self.addCleanup(lambda:[p.stop() for p in reversed(self.stack)])
        start(patch.multiple(m,STATE_PATH=self.root/'sent.json',HISTORY_PATH=self.root/'history.json',HEALTH_PATH=self.root/'health.json'))
        start(patch.dict('os.environ',{'GITHUB_OUTPUT':''}))
        self.sender=start(patch.object(m,'send_tg'))
        start(patch.object(m,'send_reports'))
        start(patch.object(m,'backfill_pdf_links'))
        start(patch.object(m,'ANTHROPIC_API_KEY',''))
        original=m.write_html
        start(patch.object(m,'write_html',side_effect=lambda h,e:original(h,e,self.root/'report.html')))

    def test_partial_failure_alert_once_then_recovery(self):
        scrape=self.start(patch.object(m,'scrape_all',return_value=([],['NICE: 원본 2행 / 파싱 1행 불일치'],None)))
        self.assertEqual(m.main(),1)
        message=self.sender.call_args.args[0]
        self.assertIn('누락 감지',message)
        self.assertNotIn('신규 등급변동 내역이 없습니다',message)
        self.assertEqual(m.main(),1)
        self.assertEqual(self.sender.call_count,1)
        scrape.return_value=([],[],None)
        self.assertEqual(m.main(),0)
        self.assertIn('해소',self.sender.call_args.args[0])
        self.assertEqual(checks.read_json(m.HEALTH_PATH,{})['last_success'],m.TODAY)

    def test_late_unsent_event_triggers_warning_and_is_recovered(self):
        checks.write_json(m.HEALTH_PATH,{'last_success':'2026-10-27'})
        self.start(patch.object(m,'scrape_all',return_value=([dict(ROW)],[],None)))
        self.assertEqual(m.main(),1)
        self.assertIn('미발송 공시 발견',self.sender.call_args.args[0])
        self.assertIn(m.notification_key(ROW),checks.read_json(m.STATE_PATH,{}))
        self.assertIn(m.item_key(ROW),checks.read_json(m.HISTORY_PATH,{}))

    def test_corrupt_history_is_not_erased(self):
        m.HISTORY_PATH.write_text('{broken',encoding='utf-8')
        self.assertEqual(m.main(),1)
        self.assertEqual(m.HISTORY_PATH.read_text(encoding='utf-8'),'{broken')
        self.assertIn('누락 방지 경고',self.sender.call_args.args[0])

    def test_telegram_failure_does_not_mark_sent(self):
        self.start(patch.object(m,'scrape_all',return_value=([dict(ROW)],[],None)))
        self.sender.side_effect=RuntimeError('telegram unavailable')
        with self.assertRaises(RuntimeError):
            m.main()
        self.assertFalse(m.STATE_PATH.exists())

    def test_report_lost_row_triggers_telegram_warning(self):
        self.start(patch.object(m,'scrape_all',return_value=([dict(ROW)],[],None)))
        def broken(h,e):
            path=self.root/'report.html'
            path.write_text('const DATA = {"records":[]};\n',encoding='utf-8')
            return path
        self.start(patch.object(m,'write_html',side_effect=broken))
        self.assertEqual(m.main(),1)
        self.assertIn('웹 리포트 누락',self.sender.call_args.args[0])

    def test_changed_saved_watch_status_is_detected(self):
        checks.write_json(m.HISTORY_PATH,{m.item_key(ROW):dict(ROW,new_outlook='안정적')})
        checks.write_json(m.STATE_PATH,{m.notification_key(ROW):m.TODAY})
        path=self.root/'report.html'
        path.write_text('const DATA = '+json.dumps({'records':[ROW]})+';\n',encoding='utf-8')
        with self.assertRaises(checks.IntegrityError):
            checks.verify_persisted([ROW],m.HISTORY_PATH,m.STATE_PATH,path,m.item_key,m.notification_key,m.RECORD_FIELDS)


class DeliveryConfigurationTests(unittest.TestCase):
    def test_missing_telegram_configuration_is_a_failure(self):
        with patch.multiple(m, TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID=''), self.assertRaises(RuntimeError):
            m.send_tg('신용등급 알림')
