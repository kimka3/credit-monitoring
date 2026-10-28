"""Independent source accounting, persisted-result checks, and alert state."""
import hashlib
import json
import re
from pathlib import Path


class IntegrityError(RuntimeError):
    pass


def read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise IntegrityError(f'{path.name}: JSON 객체가 아닙니다')
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')
    temp.replace(path)


def volume_issues(health, stats, cutoff):
    issues = []
    for label, current in stats.items():
        previous = health.get('sources', {}).get(label, {})
        old, new = previous.get('parsed', 0), current.get('parsed', 0)
        if ((old >= 20 and new < old * .2) or (old > 0 and new == 0)) and previous.get('latest_date', '') >= cutoff:
            issues.append(f'{label}: 수집량 급감/누락 의심 (직전 {old}행 → 현재 {new}행)')
    return issues


def json_tables(data, label, required_tables=()):
    if not isinstance(data, dict):
        raise IntegrityError(f'{label}: data 블록 형식 변경')
    missing = [name for name in required_tables
               if not isinstance(data.get(name), dict) or 'Data' not in data[name]]
    if missing:
        raise IntegrityError(f'{label}: 상품별 Data 블록 누락: {", ".join(sorted(missing))}')
    tables = [v['Data'] for v in data.values() if isinstance(v, dict) and 'Data' in v]
    if not tables or any(not isinstance(t, list) for t in tables):
        raise IntegrityError(f'{label}: Data 배열 누락/형식 변경')
    if any(not isinstance(row, dict) for table in tables for row in table):
        raise IntegrityError(f'{label}: 데이터 행 형식 변경')
    return [r for table in tables for r in table]


def validate_html_count(html, count, label):
    # NICE 화면의 공시 건수는 파서와 독립적인 검산 기준이다.
    advertised = re.findall(r'<h2\b[^>]*>.*?<strong>\s*([\d,]+)\s*건\s*</strong>.*?</h2>', html, re.S)
    if not advertised:
        raise IntegrityError(f'{label}: 원본 공시 건수 헤더가 없습니다')
    total = sum(int(x.replace(',', '')) for x in advertised)
    if total != count:
        raise IntegrityError(f'{label}: 원본 {total}행 / 파싱 {count}행 불일치')
    if total == 0 and '등록된 정보가 없습니다.' not in html:
        raise IntegrityError(f'{label}: 0건을 확인할 원본 표시가 없습니다')


def validate_kis_count(html, count):
    advertised = re.findall(r'<h2\b[^>]*>.*?<em>\s*([\d,]+)\s*</em>.*?</h2>', html, re.S)
    if not advertised:
        raise IntegrityError('한국신용평가: 원본 공시 건수 헤더 누락')
    total = sum(int(x.replace(',', '')) for x in advertised)
    if total != count:
        raise IntegrityError(f'한국신용평가: 원본 {total}행 / 파싱 대상 {count}행 불일치')


def expected_changes(rows):
    """Production filter_changes를 호출하지 않고 변동 증거를 검산한다."""
    for r in rows:
        pr, nr = r.get('prev_rating'), r.get('new_rating')
        po, no = r.get('prev_outlook'), r.get('new_outlook')
        code = str(r.get('change_code') or '').strip()
        if (r.get('event_type') == 'watch_registration'
                or code not in ('', '0')
                or (pr and nr and pr != nr)
                or (po and no and po != no)
                or ('prev_watch' in r and r.get('prev_watch') != r.get('new_watch'))):
            yield r


def verify_selection(rows, selected, key, label):
    actual = {key(r) for r in selected}
    missing = [r for r in expected_changes(rows) if key(r) not in actual]
    if missing:
        names = ', '.join(f"{r.get('company')} [{r.get('date')}]" for r in missing[:5])
        raise IntegrityError(f'{label}: 변동 필터 누락 {len(missing)}행: {names}')


def verify_persisted(rows, history_path, sent_path, report_path, key, notification_key, fields):
    history = read_json(history_path, {})
    sent = read_json(sent_path, {})
    html = Path(report_path).read_text(encoding='utf-8')
    match = re.search(r'const DATA = (\{.*?\});\s*\n', html, re.S)
    if not match:
        raise IntegrityError('웹 리포트 DATA 블록이 없습니다')
    public = {key(r): r for r in json.loads(match[1])['records']}
    for r in rows:
        k = key(r)
        for target, records in [('변동 이력', history), ('웹 리포트', public)]:
            saved = records.get(k)
            if saved is None or any(saved.get(f, '') != r.get(f, '') for f in fields if f in r):
                raise IntegrityError(f"{target} 누락/내용 불일치: {r['company']} [{r['date']}]")
        if notification_key(r) not in sent:
            raise IntegrityError(f"발송 이력 누락: {r['company']} [{r['date']}]")


def incident_key(message):
    if '수집량 급감/누락 의심' in message:
        message = message.split(' (직전')[0]
    return hashlib.sha256(message.encode('utf-8')).hexdigest()[:20]


def pending_alerts(health, issues):
    old = health.get('active_issues', {})
    return [message for message in issues if incident_key(message) not in old]


def update_health(health, issues, today, stats):
    health['active_issues'] = {incident_key(x): x for x in issues}
    health['last_checked'] = today
    baseline = dict(health.get('sources', {}))
    baseline.update({label: value for label, value in stats.items()
                     if not any(message.startswith(label + ':') for message in issues)})
    health['sources'] = baseline
    health['latest_sources'] = stats
    if not issues:
        health['last_success'] = today
    return health
