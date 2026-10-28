import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import credit_monitor_daily as m


NICE = '''<table><tr><th>기업명</th><th>종류</th><th>등급</th><th>등급감시부호</th><th>등록일</th><th>WATCH 사유</th></tr>
<tr><td><a href="javascript:goView('CORP','2764223')">(주)애큐온캐피탈</a></td><td>CP</td><td>A2</td><td>↑</td><td>2026.10.01</td><td>대주주/경영권 변경</td></tr>
<tr><td>(주)애큐온캐피탈</td><td>STB</td><td>A2</td><td>↑</td><td>2026.10.01</td><td>대주주/경영권 변경</td></tr>
<tr><td>오래된회사</td><td>SB</td><td>A</td><td>↓</td><td>2026.08.01</td><td>과거</td></tr></table>'''
KR = dict(data=dict(data1=dict(Data=[dict(COMP_NM='(주)애큐온캐피탈', GRD_NM='A2',
    RGDT='2026.10.01', CRAL_CD='01', CRAL_NM='긍정적 검토', BOND_KIND_KRNM='기업어음')]),data2=dict(Data=[])))
OLD = dict(source='한국기업평가', company='(주)애큐온캐피탈', date='2026.10.01',
           prev_rating='A2',new_rating='A2', prev_outlook='',new_outlook='',kind='변경',first_seen='2026-10-02')


class Session:
    def __init__(self, html='', data=None):
        self.text, self.data = html, data

    def get(self, *a, **kw):
        return self

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.dates = patch.multiple(m, NOW=datetime(2026,10,28,tzinfo=m.KST), TODAY='2026-10-28', WATCH_FROM='2026.09.28')
        self.dates.start()
        self.addCleanup(self.dates.stop)

    def test_nice_retains_instruments_and_registration(self):
        rows = m.fetch_nice_watch(Session(NICE))
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(m.item_key(rows[0]), m.item_key(rows[1]))
        self.assertEqual(rows[0]['cmp_cd'], '2764223')
        self.assertTrue(all(r['kind']=='검토변경' for r in rows))
        self.assertIn('상향검토 등록 (CP)', m.describe_change(rows[0]))

    def test_bad_nice_row_fails_instead_of_silently_dropping(self):
        for html in [NICE.replace('<td>↑</td>', '<td>?</td>'), NICE.replace('<td>CP</td>',''), '<html>blocked</html>']:
            with self.subTest(html=html[:40]), self.assertRaises(m.StructureError):
                m.fetch_nice_watch(Session(html))

    def test_empty_watch_list_is_valid_with_header(self):
        html=NICE.split('<tr><td>')[0]+'<tr><td colspan="6">등록된 정보가 없습니다.</td></tr></table>'
        self.assertEqual(m.fetch_nice_watch(Session(html)), [])

    def test_kr_enrichment_preserves_history_key_and_sent_marker(self):
        with patch.object(m,'load_history',return_value={m.item_key(OLD):OLD}):
            row=m.fetch_kr_watch(Session(data=KR))[0]
            self.assertEqual(m.item_key(row),m.item_key(OLD))
            self.assertNotEqual(m.notification_key(row),m.item_key(OLD))
            self.assertEqual(row['kind'],'검토변경')
            self.assertEqual(row['new_watch'],'상향검토')
        with patch.object(m,'load_history',return_value={m.item_key(row):row}):
            again=m.fetch_kr_watch(Session(data=KR))[0]
            self.assertEqual(m.notification_key(again),m.notification_key(row))

    def test_kr_bad_schema_fails(self):
        with self.assertRaises(m.IntegrityError):
            m.fetch_kr_watch(Session(data={'data':{}}))

    def test_active_watch_alone_is_not_a_new_change(self):
        row=dict(OLD,new_watch='상향검토',change_code='0')
        self.assertEqual(m.filter_changes([row]),[])
        row['change_code']='3'
        self.assertEqual(m.filter_changes([row])[0]['kind'],'검토변경')

    def test_grade_change_with_watch_remains_grade_change(self):
        row=dict(OLD,prev_rating='A2+',new_watch='상향검토',change_code='1')
        self.assertEqual(m.filter_changes([row])[0]['kind'],'등급하향')

    def test_watch_codes_include_no_watch(self):
        self.assertEqual(m.watch_name('00'),'')
        self.assertEqual(m.watch_name('01'),'상향검토')
        self.assertEqual(m.watch_name('02'),'하향검토')

    def test_notification_and_history_update_once(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root=Path(temp)
            sp,hp=root/'sent.json',root/'history.json'
            sp.write_text(json.dumps({m.item_key(OLD):'2026-10-02'}),encoding='utf-8')
            hp.write_text(json.dumps({m.item_key(OLD):OLD}),encoding='utf-8')
            with patch.multiple(m,STATE_PATH=sp,HISTORY_PATH=hp,HEALTH_PATH=root/'health.json'):
                kr=m.fetch_kr_watch(Session(data=KR))
                nice=m.fetch_nice_watch(Session(NICE))
                write=m.write_html
                report=root/'report.html'
                with patch.object(m,'scrape_all',return_value=(kr+nice,[],None)), \
                     patch.object(m,'send_tg') as sender, patch.object(m,'send_reports'), \
                     patch.object(m,'backfill_pdf_links'), \
                     patch.object(m,'write_html',side_effect=lambda h,e:write(h,e,report)):
                    self.assertEqual(m.main(),0)
                    self.assertIn('상향검토 등록',sender.call_args.args[0])
                    hist=json.loads(hp.read_text(encoding='utf-8'))
                    self.assertEqual(len(hist),3)
                    self.assertEqual(hist[m.item_key(OLD)]['kind'],'검토변경')
                    html=report.read_text(encoding='utf-8')
                    self.assertIn('"new_watch": "상향검토"',html)
                    self.assertEqual(m.main(),0)
                    self.assertIn('신규 등급변동 내역이 없습니다',sender.call_args.args[0])
                    self.assertEqual(len(json.loads(hp.read_text(encoding='utf-8'))),3)
