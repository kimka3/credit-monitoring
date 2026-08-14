import subprocess; subprocess.run(["pip","install","-q","anthropic"])

# ============================================================
# 신용등급 모니터링 — 3사 통합 최종 코드 (v6)
# 변경사항 (v5 -> v6):
#  - 취소 건 전면 제외 (v5의 장기등급 취소 표시가 증권사 ELB/DLB 회차 상환
#    노이즈만 대량 유입시켜 롤백)
# (v4 -> v5 누적)
#  1. 3회 재시도 로직 (30초 간격) — NICE 간헐적 ConnectTimeout 대응
#  2. NICE만 14일 조회 — 하루 실패해도 다음 날 자동으로 누락분 복구
#  3. 타임아웃 (30, 60)으로 상향
# ============================================================
import os
# --- API 키 설정 ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY","")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN","")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

import requests, json, re, time, anthropic
from datetime import datetime, timedelta
from html.parser import HTMLParser

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept": "text/html, application/json, */*",
}
TODAY = datetime.now().strftime("%Y-%m-%d")
WEEK_AGO = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
# NICE는 실패가 잦아 14일 조회 (중복은 어차피 필터링됨 -> 하루 실패해도 자동 복구)
NICE_FROM = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

TIMEOUT = (30, 60)   # (연결, 읽기)

RATING_GRADES = {"AAA","AA+","AA","AA-","A+","A","A-","BBB+","BBB","BBB-",
                 "BB+","BB","BB-","B+","B","B-","CCC","CC","C","D",
                 "A1","A2+","A2","A2-","A3+","A3","A3-"}
SHORT_TERM_GRADES = {"A1","A2+","A2","A2-","A3+","A3","A3-"}

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
    """실제 신용등급인지 확인. 'AAA (sf)' 형태 지원."""
    if not s: return False
    base = s.replace("(sf)","").replace("보증","").strip()
    return base in RATING_GRADES

def is_date(s):
    return bool(re.match(r'\d{4}\.\d{2}\.\d{2}', s))

def dedupe_and_filter_changes(results):
    """기업 단위 중복 제거 + 등급/전망 변동건만 반환"""
    seen, changed = set(), []
    for d in results:
        key = (d["company"], d["prev_rating"], d["new_rating"],
               d["prev_outlook"], d["new_outlook"])
        if key in seen: continue
        seen.add(key)
        if d.get("is_cancel"):
            changed.append(d)
        elif (d.get("change_code") and d["change_code"] not in ("", "0")) or \
             (d["prev_rating"] and d["new_rating"] and d["prev_rating"] != d["new_rating"]) or \
             (d["prev_outlook"] and d["new_outlook"] and d["prev_outlook"] != d["new_outlook"]):
            changed.append(d)
    return changed

# ============================================================
#  한국기업평가 (AJAX API)
# ============================================================
def fetch_kr(session):
    print("  [한국기업평가] 수집 중...")
    session.get("https://www.korearatings.com/cms/frCmnCon/index.do?MENU_ID=360", timeout=TIMEOUT)

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
        data=params, timeout=TIMEOUT)
    r.encoding = "utf-8"
    data = r.json()

    all_items = []
    for key, val in data.get("data", {}).items():
        if isinstance(val, dict) and "Data" in val:
            all_items.extend(val["Data"])

    results, n_cancel_skipped = [], 0
    for item in all_items:
        new_rating = (item.get("CUR_GRD_NM_ORG") or "").strip()
        prev_rating = (item.get("RBF_GRD_NM_ORG") or "").strip()

        if not new_rating:
            continue

        # 취소 건 전면 제외 (장기/단기 불문)
        # 실데이터 확인 결과, 증권사 AA 등급 취소도 대부분 ELB/DLB 개별 회차
        # 만기상환에 따른 등급 소멸이지 회사 등급 취소가 아님 -> 전량 노이즈
        if new_rating == "취소":
            n_cancel_skipped += 1
            continue
        is_cancel = False

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
            "is_cancel": is_cancel,
        })

    changed = dedupe_and_filter_changes(results)
    print(f"  [한국기업평가] 전체 {len(all_items)}건 -> 취소 제외 {n_cancel_skipped}건 -> 변동 {len(changed)}건")
    return changed

# ============================================================
#  한국신용평가 (HTML) — 헤더 기반 컬럼 매핑
# ============================================================
def fetch_kis(session):
    print("  [한국신용평가] 수집 중...")
    r = session.get("https://www.kisrating.com/ratings/hot_disclosure.do", timeout=TIMEOUT)
    r.encoding = "utf-8"
    p = TableParser(); p.feed(r.text)

    results, colmap, last_entry = [], None, None
    for row in p.rows:
        cells = [c.strip() for c in row]

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
            "is_cancel": False,
        }
        results.append(entry)
        last_entry = entry

    changed = dedupe_and_filter_changes(results)
    print(f"  [한국신용평가] 전체 {len(results)}건 -> 변동 {len(changed)}건")
    return changed

# ============================================================
#  NICE신용평가 (HTML) — 14일 조회
#  파라미터 없이 호출하면 항상 빈 결과. ratingGubn: R=등급, O=전망, RO=둘다
# ============================================================
def fetch_nice(session):
    print("  [NICE신용평가] 수집 중...")
    r = session.get(
        "https://www.nicerating.com/disclosure/ratingChangeList.do",
        params=[("today", TODAY), ("cmpCd", ""),
                ("strDate", NICE_FROM), ("endDate", TODAY),
                ("ratingGubn", "RO"), ("searchType", "0")],
        timeout=TIMEOUT)
    r.encoding = "utf-8"
    p = TableParser(); p.feed(r.text)

    results = []
    for row in p.rows:
        cells = [c.strip() for c in row]

        if "기업명" in cells or cells[:2] == ["등급", "전망"]:
            continue
        if not cells or not is_date(cells[-1]):
            continue

        n = len(cells)
        if n == 10:
            # 채권: 기업명,회차,상환순위,종류,평정,직전등급,직전전망,현재등급,현재전망,확정일
            entry = dict(company=cells[0], eval_type=cells[4],
                         prev_rating=cells[5], prev_outlook=cells[6],
                         new_rating=cells[7], new_outlook=cells[8], date=cells[9])
        elif n == 7:
            entry = dict(company=cells[0], eval_type=cells[1],
                         prev_rating=cells[2], prev_outlook=cells[3],
                         new_rating=cells[4], new_outlook=cells[5], date=cells[6])
        elif n == 5:
            entry = dict(company=cells[0], eval_type=cells[1],
                         prev_rating=cells[2], prev_outlook="",
                         new_rating=cells[3], new_outlook="", date=cells[4])
        else:
            continue

        if not is_valid_rating(entry["new_rating"]):
            continue
        if entry["prev_rating"] and not is_valid_rating(entry["prev_rating"]):
            entry["prev_rating"] = ""

        entry.update(source="NICE신용평가", change_code="", is_cancel=False)
        results.append(entry)

    changed = dedupe_and_filter_changes(results)
    print(f"  [NICE신용평가] 전체 {len(results)}건 -> 변동 {len(changed)}건 (조회 {NICE_FROM}~{TODAY})")
    return changed

# ============================================================
#  통합 수집 — 3회 재시도 (30초 간격)
# ============================================================
def scrape_all():
    session = requests.Session()
    session.headers.update(HEADERS)
    all_data, errors = [], []

    for fetcher in [fetch_kr, fetch_kis, fetch_nice]:
        last_err = None
        for attempt in range(3):
            try:
                all_data.extend(fetcher(session))
                last_err = None
                break
            except Exception as e:
                last_err = e
                print(f"  재시도 {attempt+1}/3 [{fetcher.__name__}]: {type(e).__name__}")
                if attempt < 2:
                    time.sleep(30)
        if last_err:
            print(f"  최종 실패 [{fetcher.__name__}]: {type(last_err).__name__}: {last_err}")
            errors.append(f"{fetcher.__name__}: {type(last_err).__name__}")

    print(f"\n  === 3사 합계: 변동 {len(all_data)}건, 에러 {len(errors)}건 ===")
    return all_data, errors

# ============================================================
#  Claude 분석
# ============================================================
SYSTEM = """당신은 한국 채권시장 전문 애널리스트입니다.
증권사 신탁부서에서 특정금전신탁 포트폴리오를 운용하는 팀장에게
매일 아침 신용등급 변동 브리핑을 제공합니다.
등급 하향 및 부도(D) 건은 ⚠️로 최우선 표시. 투자적격등급(BBB-이상) 중심.
금융채와 일반 회사채 구분. 같은 기업 개별 회차는 1줄로 통합.
동일 계열/시리즈에서 복수 건이 발생하면 패턴으로 묶어 언급.
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
                "■ 부도/등급 하향 (⚠️)\n  - 업체명 | 변경전→변경후 | 출처\n"
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
    # 부도/하향 우선 정렬
    def sort_key(c):
        if c.get("new_rating") == "D": return 0
        if c.get("is_cancel"): return 2
        return 1
    for c in sorted(changes, key=sort_key):
        prev = f"{c['prev_rating']}({c['prev_outlook']})" if c.get("prev_outlook") else c.get("prev_rating","")
        new = f"{c['new_rating']}({c['new_outlook']})" if c.get("new_outlook") else c.get("new_rating","")
        if not prev: prev = "신규"
        mark = "⚠️ " if c.get("new_rating") == "D" else ""
        lines.append(f"- {mark}{c['company']} | {prev} -> {new} | {c['source']} [{c.get('date','')}]")
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
print("✅ 3사 통합 코드 (v6) 로드 완료!")
print(f"   수집 기간: {WEEK_AGO} ~ {TODAY} (NICE: {NICE_FROM} ~ {TODAY})")
print("   재시도 3회 / 타임아웃 (30,60)")
print(f"📊 신용등급 모니터링 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print("="*60)
print("\n[1/3] 3사 데이터 수집...")
data, errors = scrape_all()

print("\n[2/3] AI 분석...")
briefing = analyze(data)
if errors:
    briefing += "\n\n⚠️ 일부 수집 실패: " + ", ".join(errors)

print("\n[3/3] 알림 전송...")
send_tg(briefing)
print("\n✅ 완료!")
