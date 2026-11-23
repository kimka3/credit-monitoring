"""Explicit manual backfill from a fresh, reviewed public NICE Watch snapshot.

Routine collection never reads snapshots. A partial backfill does not clear live
collection warnings or advance the last successful monitoring date.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import credit_monitor_daily as m
from monitor_checks import IntegrityError, verify_persisted
from nice_feed import DisclosureSession


def snapshot_rows(snapshot, now):
    if snapshot.get('source_url') != m.NICE_WATCH_URL:
        raise IntegrityError('NICE Watch 공식 원본 URL 불일치')
    captured = datetime.fromisoformat(snapshot['captured_at'])
    if captured.tzinfo is None or not timedelta(0) <= now - captured <= timedelta(hours=24):
        raise IntegrityError('NICE Watch 원본은 24시간 이내 수집한 자료만 복구할 수 있습니다')
    html = snapshot['html']
    if hashlib.sha256(html.encode('utf-8')).hexdigest() != snapshot['sha256']:
        raise IntegrityError('NICE Watch 원본 체크섬 불일치')

    class Cached:
        text = html
        def get(self, *args, **kwargs):
            return self
        def raise_for_status(self):
            pass

    return m.fetch_nice_watch(Cached())


def recover(snapshot):
    return recover_rows(snapshot_rows(snapshot, datetime.now(timezone.utc)))


def recover_bundle(encoded):
    with m.requests.Session() as live:
        live.headers.update(m.HEADERS)
        source = DisclosureSession(encoded, live, m.NOW, m.NICE_FROM, m.TODAY,
                                   m.NICE_LIST_URL, m.NICE_WATCH_URL)
        rows = []
        for fetch in (m.fetch_nice, m.fetch_nice_sf, m.fetch_nice_exposure, m.fetch_nice_watch):
            rows.extend(fetch(source))
    return recover_rows(rows)


def recover_rows(rows):
    state, history = m.load_state(), m.load_history()
    health = m.read_json(m.HEALTH_PATH, {})
    issues = list(health.get('active_issues', {}).values())
    if not issues:
        raise IntegrityError('활성 수집 장애가 없습니다. 정상 모니터로 재조회하세요')
    fresh = [row for row in rows if m.notification_key(row) not in state]
    if fresh:
        m.send_tg('⚠️ NICE 접속 장애 보충 알림\n공식 사이트 원본으로 확인한 미발송 내역입니다.\n\n'
                  + m.format_plain(fresh)
                  + '\n\nGitHub의 NICE 자동 수집 접속 오류는 아직 해결되지 않았습니다.')
        for row in fresh:
            state[m.notification_key(row)] = m.TODAY
        m.save_state(state)
    for row in rows:
        record = history.setdefault(m.item_key(row), {'first_seen': m.TODAY})
        record.update({k: row[k] for k in m.RECORD_FIELDS if k in row})
    m.save_history(history)
    report = m.write_html(history, issues)
    verify_persisted(rows, m.HISTORY_PATH, m.STATE_PATH, report,
                     m.item_key, m.notification_key, m.RECORD_FIELDS)
    m.github_output('report_ready', 'true')
    m.mark_failure_handled()
    print(f'수동 보충 검증 완료: 원본 변동 {len(rows)}건, 신규 발송 {len(fresh)}건. 자동 수집 경고 유지.')
    # The workflow must remain failed while live collection is unavailable.
    return 1


if __name__ == '__main__':
    if sys.argv[1:] == ['--bundle']:
        sys.exit(recover_bundle(os.environ['NICE_DISCLOSURES']))
    if len(sys.argv) != 2 or Path(sys.argv[1]).name != sys.argv[1] or not sys.argv[1].endswith('.json'):
        raise SystemExit('ops/recovery/ 아래 JSON 파일명 하나를 지정하세요')
    path = Path(__file__).resolve().parent / 'recovery' / sys.argv[1]
    sys.exit(recover(json.loads(path.read_text(encoding='utf-8'))))
