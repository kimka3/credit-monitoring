import subprocess; subprocess.run(["pip","install","-q","anthropic"])

# ============================================================
# 신용등급 모니터링 — 3사 통합 최종 코드 (v4)
# 변경사항 (v3 -> v4):
#  1. 한신평: 고정 컬럼 인덱스 -> 헤더 기반 컬럼 매핑 (ABS 등 섹션별 구조 차이 대응)
#  2. NICE: 필수 파라미터 추가 (strDate/endDate/ratingGubn=RO 등) — 파라미터 없이는 빈 결과 반환됨
#  3. NICE: 채권(10컬럼)/기업어음(5컬럼)/기업신용평가(7컬럼) 섹션별 행 길이 분기
#  4. "수집 실패"와 "변동 없음"을 구분해서 알림 (에러 발생 시 텔레그램으로 에러 통지)
# ============================================================
import os
# --- API 키 설정 ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY","")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN","")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

import requests, json, re, anthropic
from datetime import datetime, timedelta
from html.parser import HTMLParser

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept": "text/html, application/json, */*",
}
TODAY = datetime.now().strftime("%Y-%m-%d")
WEEK_AGO = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

RATING_GRADES = {"AAA","AA+","AA","AA-","A+","A","A-","BBB+","BBB","BBB-",
                 "BB+","BB","BB-","B+","B","B-","CCC","CC","C","D",
                 "A1","A2+","A2","A2-","A3+","A3","A3-"}

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._in = [], [], "", False
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self._row = []
        elif tag in ("td", "th"): self._in = True; self._cell = ""
    def handle_endtag(self, tag):
        if tag in ("td", "th"): self._row.append(self._cell.strip()); self._in = False
        elif tag == "tr" and self._row: self.rows.append(self._row)
    def handle_data(self, data):
        if self._in: self._cell += data

def is_valid_rating(s):
    """실제 신용등급인지 확인 (숫자, 날짜, 헤더 텍스트 제외). 'AAA (sf)' 형태 지원."""
    if not s: return False
    base = s.replace("(sf)","").replace("보증","").strip()
    return base in RATING_GRADES

def is_date(s):
    """날짜 형식인지 확인"""
    return bool(re.match(r'\d{4}\.\d{2}\.\d{2}', s))

def dedupe_and_filter_changes(results):
    """기업 단위 중복 제거 + 등급/전망 변동건만 반환 (공통 로직)"""
    seen, changed = set(), []
    for d in results:
        key = (d["company"], d["prev_rating"], d["new_rating"],
               d["prev_outlook"], d["new_outlook"])
        if key in seen: continue
        seen.add(key)
        if (d.get("change_code") and d["change_code"] not in ("", "0")) or \
           (d["prev_rating"] and d["new_rating"] and d["prev_rating"] != d["new_rating"]) or \
           (d["prev_outlook"] and d["new_outlook"] and d["prev_outlook"] != d["new_outlook"]):
            changed.append(d)
    return changed

# ============================================================
#  한국기업평가 (AJAX API) — 취소 제외  [v3과 동일, 정상 작동 확인됨]
# ============================================================
def fetch_kr(session):
    print("  [한국기업평가] 수집 중...")
    session.get("https://www.korearatings.com/cms/frCmnCon/index.do?MENU_ID=360", timeout=15)

    params = [
        ("MENU_ID", "360"), ("CONTENTS_NO", "1"), ("SITE_NO", "2"), ("COMP_CD", ""),
        ("STDT", WEEK_AGO), ("ENDT", TODAY),
        ("SVCTY_CD", "01"), ("SVCTY_CD", "07"), ("SVCTY_CD", "02"),
        ("SVCTY_CD", "03"), ("SVCTY_CD", "10"), ("SVCTY_CD", "11"),
        ("SVCTY_CD", "05"), ("SVCTY_CD", "09"), ("SVCTY_CD", "04"),
        ("SVCTY_CD", "06"), ("SVCTY_CD", "08"),
    ]

    r = session.post(
        "https://www.korearatings.com/ajaxf/frDisclosureSvc/getRatingDisclosureList.do",
        headers={
            "Referer": "https://www.korearatings.com/cms/frCmnCon/index.do?MENU_ID=360",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        data=params, timeout=30)
    r.encoding = "utf-8"
    data = r.json()

    all_items = []
    for key, val in data.get("data", {}).items():
        if isinstance(val, dict) and "Data" in val:
            all_items.extend(val["Data"])

    results = []
    for item in all_items:
        new_rating = (item.get("CUR_GRD_NM_ORG") or "").strip()
        prev_rating = (item.get("RBF_GRD_NM_ORG") or "").strip()

        # 취소 건 제외
        if not new_rating or new_rating == "취소":
            continue

        results.append({
            "source": "한국기업평가",
            "company": (item.get("COMP_NM") or "").strip(),
            "prev_rating": prev_rating,
            "new_rating": new_rating,
            "prev_outlook": (item.get("BFR_OL_NM") or "").strip(),
            "new_outlook": (item.get("OL_NM") or "").strip(),
            "date": item.get("EVAL_DT") or item.get("DSCLS_DTTM") or "",
            "eval_type": (item.get("EVAL_DIV_NM") or "").strip(),
            "change_code": item.get("GR_CHN_DVCD", "0"),
        })

    changed = dedupe_and_filter_changes(results)
    print(f"  [한국기업평가] 전체 {len(all_items)}건 -> 취소 제외 {len(results)}건 -> 변동 {len(changed)}건")
    return changed

# ============================================================
#  한국신용평가 (HTML) — v4: 헤더 기반 컬럼 매핑
#  페이지에 채권/CP/ABS/신용공여 등 컬럼 구조가 다른 테이블이 혼재하므로
#  '회사명'이 포함된 헤더 행을 만날 때마다 컬럼 위치를 갱신한다.
# ============================================================
def fetch_kis(session):
    print("  [한국신용평가] 수집 중...")
    r = session.get("https://www.kisrating.com/ratings/hot_disclosure.do", timeout=15)
    r.encoding = "utf-8"
    p = TableParser(); p.feed(r.text)

    results, colmap, last_entry = [], None, None
    for row in p.rows:
        cells = [c.strip() for c in row]

        # 헤더 행이면 컬럼 매핑 갱신
        if "회사명" in cells:
            colmap = {}
            for i, c in enumerate(cells):
                if c == "회사명": colmap["company"] = i
                elif c in ("직전등급", "직전"): colmap["prev"] = i
                elif c in ("현재등급", "현재"): colmap["cur"] = i
                elif c == "평가일": colmap["date"] = i
                elif c == "평가종류": colmap["type"] = i
            last_entry = None
            continue

        # Outlook 행(짧은 행)이면 직전 데이터 행에 병합
        if len(cells) <= 4:
            if last_entry and len(cells) >= 3:
                last_entry["prev_outlook"] = cells[0]
                last_entry["new_outlook"] = cells[2]
            continue

        if not colmap or "cur" not in colmap or "company" not in colmap:
            continue
        if max(colmap.values()) >= len(cells):
            continue

        company = cells[colmap["company"]]
        new_r = cells[colmap["cur"]]
        prev_r = cells[colmap["prev"]] if "prev" in colmap else ""
        date = cells[colmap["date"]] if "date" in colmap else ""

        if not company or not is_valid_rating(new_r) or not is_date(date):
            last_entry = None
            continue

        entry = {
            "source": "한국신용평가",
            "company": company,
            "prev_rating": prev_r if is_valid_rating(prev_r) else "",
            "new_rating": new_r,
            "prev_outlook": "",
            "new_outlook": "",
            "date": date,
            "eval_type": cells[colmap["type"]] if "type" in colmap else "",
            "change_code": "",
        }
        results.append(entry)
        last_entry = entry

    changed = dedupe_and_filter_changes(results)
    print(f"  [한국신용평가] 전체 {len(results)}건 -> 변동 {len(changed)}건")
    return changed

# ============================================================
#  NICE신용평가 (HTML) — v4: 필수 파라미터 추가 + 섹션별 행 길이 분기
#  파라미터 없이 호출하면 항상 빈 결과가 반환됨 (2026.07 확인).
#  ratingGubn: R=등급변동, O=전망변동, RO=둘 다
# ============================================================
def fetch_nice(session):
    print("  [NICE신용평가] 수집 중...")
    r = session.get(
        "https://www.nicerating.com/disclosure/ratingChangeList.do",
        params=[("today", TODAY), ("cmpCd", ""),
                ("strDate", WEEK_AGO), ("endDate", TODAY),
                ("ratingGubn", "RO"), ("searchType", "0")],
        timeout=15)
    r.encoding = "utf-8"
    p = TableParser(); p.feed(r.text)

    results = []
    for row in p.rows:
        cells = [c.strip() for c in row]

        # 헤더/서브헤더 행 스킵
        if "기업명" in cells or cells[:2] == ["등급", "전망"]:
            continue
        # 마지막 컬럼이 날짜가 아니면 데이터 행이 아님
        if not cells or not is_date(cells[-1]):
            continue

        n = len(cells)
        if n == 10:
            # 채권: 기업명,회차,상환순위,종류,평정,직전등급,직전전망,현재등급,현재전망,확정일
            entry = dict(company=cells[0], eval_type=cells[4],
                         prev_rating=cells[5], prev_outlook=cells[6],
                         new_rating=cells[7], new_outlook=cells[8], date=cells[9])
        elif n == 7:
            # 기업신용평가/보험금지급능력(추정): 기업명,평정,직전등급,직전전망,현재등급,현재전망,확정일
            entry = dict(company=cells[0], eval_type=cells[1],
                         prev_rating=cells[2], prev_outlook=cells[3],
                         new_rating=cells[4], new_outlook=cells[5], date=cells[6])
        elif n == 5:
            # 기업어음/전단채: 기업명,평정,직전등급,현재등급,확정일 (전망 없음)
            entry = dict(company=cells[0], eval_type=cells[1],
                         prev_rating=cells[2], prev_outlook="",
                         new_rating=cells[3], new_outlook="", date=cells[4])
        else:
            continue

        # 등급 유효성 검증 (헤더 잔재/잘못된 행 방어)
        if not is_valid_rating(entry["new_rating"]):
            continue
        if entry["prev_rating"] and not is_valid_rating(entry["prev_rating"]):
            entry["prev_rating"] = ""

        entry.update(source="NICE신용평가", change_code="")
        results.append(entry)

    changed = dedupe_and_filter_changes(results)
    print(f"  [NICE신용평가] 전체 {len(results)}건 -> 변동 {len(changed)}건")
    return changed

# ============================================================
#  통합 수집 — v4: 에러를 별도로 수집해서 '수집 실패'와 '변동 없음' 구분
# ============================================================
def scrape_all():
    session = requests.Session()
    session.headers.update(HEADERS)
    all_data, errors = [], []
    for fetcher in [fetch_kr, fetch_kis, fetch_nice]:
        try:
            all_data.extend(fetcher(session))
        except Exception as e:
            name = fetcher.__name__
            print(f"  에러 [{name}]: {type(e).__name__}: {e}")
            errors.append(f"{name}: {type(e).__name__}: {e}")
    print(f"\n  === 3사 합계: 변동 {len(all_data)}건, 에러 {len(errors)}건 ===")
    return all_data, errors

# ============================================================
#  Claude 분석
# ============================================================
SYSTEM = """당신은 한국 채권시장 전문 애널리스트입니다.
증권사 신탁부서에서 특정금전신탁 포트폴리오를 운용하는 팀장에게
매일 아침 신용등급 변동 브리핑을 제공합니다.
등급 하향 건은 ⚠️로 최우선 표시. 투자적격등급(BBB-이상) 중심.
금융채와 일반 회사채 구분. 같은 기업 개별 회차는 1줄로 통합.
간결한 한국어, 최대 2000자."""

def analyze(changes):
    today = datetime.now().strftime("%Y-%m-%d")
    if not changes:
        return f"📊 [{today}] 신용등급 변동 브리핑\n\n✅ 금일 신규 등급변동 내역이 없습니다."
    if not ANTHROPIC_API_KEY or not ANTHROPIC_API_KEY.startswith("sk-"):
        return _format_plain(changes)
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        r = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=2000, system=SYSTEM,
            messages=[{"role": "user", "content":
                f"오늘: {today}\n\n등급변동 데이터:\n{json.dumps(changes, ensure_ascii=False, indent=2)}\n\n"
                "형식:\n📊 [날짜] 신용등급 변동 브리핑\n"
                "■ 등급 하향 (⚠️)\n  - 업체명 | 변경전→변경후 | 출처\n"
                "■ 등급 상향\n  - 업체명 | 변경전→변경후 | 출처\n"
                "■ Outlook 변경\n■ 신탁 포트폴리오 시사점 (2-3줄)\n"
                "빈 카테고리는 생략. 2000자 이내."}])
        return r.content[0].text
    except Exception as e:
        print(f"  Claude 에러: {e}")
        return _format_plain(changes)

def _format_plain(changes):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📊 [{today}] 신용등급 변동 내역 ({len(changes)}건)\n"]
    for c in changes:
        prev = f"{c['prev_rating']}({c['prev_outlook']})" if c.get("prev_outlook") else c.get("prev_rating","")
        new = f"{c['new_rating']}({c['new_outlook']})" if c.get("new_outlook") else c.get("new_rating","")
        if not prev: prev = "신규"
        lines.append(f"- {c['company']} | {prev} -> {new} | {c['source']} [{c.get('date','')}]")
    return "\n".join(lines)

def send_tg(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n" + "="*60)
        print(msg)
        print("="*60)
        return
    for i in range(0, len(msg), 4000):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg[i:i+4000],
                      "disable_web_page_preview": True}, timeout=10)
            print("  텔레그램 전송 성공!" if r.status_code == 200 else f"  텔레그램 실패: {r.text}")
        except Exception as e:
            print(f"  텔레그램 에러: {e}")

# ============================================================
#  실행
# ============================================================
print("✅ 3사 통합 최종 코드 (v4) 로드 완료!")
print(f"   수집 기간: {WEEK_AGO} ~ {TODAY}")
print("   한신평: 헤더 기반 컬럼 매핑 (ABS 섹션 포함)")
print("   NICE: 기간/ratingGubn=RO 파라미터 적용")
print(f"📊 신용등급 모니터링 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print("="*60)
print("\n[1/3] 3사 데이터 수집...")
data, errors = scrape_all()

print("\n[2/3] AI 분석...")
# v4: '변동 없음'과 '수집 실패'를 구분
if errors:
    err_note = "⚠️ 일부 수집 실패:\n" + "\n".join(f"  - {e}" for e in errors)
    briefing = analyze(data)  # 수집된 것이라도 분석
    briefing = briefing + "\n\n" + err_note
else:
    briefing = analyze(data)  # data가 비어도 '변동 없음' 메시지가 정상 출력됨

print("\n[3/3] 알림 전송...")
send_tg(briefing)
print("\n✅ 완료!")
