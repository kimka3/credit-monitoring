import unittest
from unittest.mock import Mock

import requests

import credit_monitor_daily as m


def record(source, company):
    return {'source': source, 'company': company, 'date': '2026.11.18', 'new_rating': 'A'}


class Resp:
    def __init__(self, text=''):
        self.text = text
        self.encoding = None

    def raise_for_status(self):
        pass

    def json(self):
        return {'data': {}}


class PdfBackfillTests(unittest.TestCase):
    def test_blocked_lookup_is_retried_on_the_next_run(self):
        # 2026-11-19처럼 평가사가 접속을 막으면 의견서 유무를 알 수 없다.
        hist = {'n': record('NICE신용평가', '가나다(주)')}
        session = Mock()
        session.get.side_effect = requests.HTTPError('400 Client Error: Bad Request')
        m.backfill_pdf_links(session, hist)
        self.assertNotIn('pdf_checked', hist['n'])

    def test_completed_lookup_without_opinion_is_not_repeated(self):
        hist = {'n': record('NICE신용평가', '가나다(주)')}
        session = Mock()
        session.get.return_value = Resp('<table><tr><td>목록</td></tr></table>')
        m.backfill_pdf_links(session, hist)
        self.assertEqual(hist['n']['pdf_checked'], m.TODAY)

    def test_only_sources_whose_lookup_completed_are_marked(self):
        hist = {'k': record('한국신용평가', '가나다'), 'n': record('NICE신용평가', '라마바(주)')}
        session = Mock()
        session.post.return_value = Resp('')
        session.get.side_effect = requests.HTTPError('400 Client Error: Bad Request')
        m.backfill_pdf_links(session, hist)
        self.assertEqual(hist['k']['pdf_checked'], m.TODAY)
        self.assertNotIn('pdf_checked', hist['n'])


if __name__ == '__main__':
    unittest.main()
