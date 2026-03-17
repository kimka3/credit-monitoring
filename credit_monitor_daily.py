import subprocess; subprocess.run(["pip","install","-q","anthropic"])

# ============================================================
# 신용등급 모니터링 — 3사 통합 최종 코드 (v3)
# ============================================================

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
    """실제 신용등급인지 확인 (숫자, 날짜, 헤더 텍스트 제외)"""
    if not s: return False
    base = s.replace("(sf)","").replace("보증","").strip()
    return base in RATING_GRADES

def is_date(s):
    """날짜 형식인지 확인"""
    return bool(re.match(r'\d{4}\.\d{2}\.\d{2}', s))

# ============================================================
#  한국기업평가 (AJAX API) — 취소 제외
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

        # ★ 취소 건 제외
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

    # 기업 단위 중복 제거
    seen = set()
    unique = []
    for d in results:
        key = (d["company"], d["prev_rating"], d["new_rating"], d["prev_outlook"], d["new_outlook"])
        if key not in seen:
            seen.add(key)
            unique.append(d)

    # 실제 변동건
    changed = [d for d in unique
               if d["change_code"] != "0"
               or (d["prev_rating"] and d["new_rating"] and d["prev_rating"] != d["new_rating"])
               or (d["prev_outlook"] and d["new_outlook"] and d["prev_outlook"] != d["new_outlook"])]

    print(f"  [한국기업평가] 전체 {len(all_items)}건 -> 취소 제외 {len(results)}건 -> 변동 {len(changed)}건")
    return changed

# ============================================================
#  한국신용평가 (HTML) — 헤더/잘못된 행 필터링
# ============================================================
def fetch_kis(session):
    print("  [한국신용평가] 수집 중...")
    r = session.get("https://www.kisrating.com/ratings/hot_disclosure.do", timeout=15)
    r.encoding = "utf-8"
    p = TableParser(); p.feed(r.text); rows = p.rows

    results = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if len(row) >= 8:
            company = row[1].strip()
            prev_r = row[4].strip()
            new_r = row[6].strip()
            date = row[7].strip()

            # ★ 헤더 행 제외 (회사명, 직전등급, 현재등급 등)
            if company in ("회사명", "", "Outlook") or \
               prev_r in ("직전등급", "직전", "평가종류", "발행액(억원)", "공여기관", "발행액(억원)(회사명)") or \
               new_r in ("현재등급", "현재", "평가일", "만기일", "약정만료일"):
                i += 1
                continue

            # ★ 날짜 컬럼에 실제 날짜가 있는지 확인
            if not is_date(date):
                i += 1
                continue

            # ★ 등급이 실제 신용등급인지 확인 (숫자나 "본" 같은 것 제외)
            new_is_rating = is_valid_rating(new_r)
            prev_is_rating = is_valid_rating(prev_r) or prev_r == ""

            if not new_is_rating:
                i += 1
                continue

            entry = {
                "source": "한국신용평가",
                "company": company,
                "prev_rating": prev_r if prev_is_rating else "",
                "new_rating": new_r,
                "prev_outlook": "",
                "new_outlook": "",
                "date": date,
                "eval_type": row[3].strip(),
                "change_code": "",
            }
            # 다음 행이 Outlook 행(3컬럼)이면 병합
            if i + 1 < len(rows) and len(rows[i+1]) <= 4:
                ol = rows[i+1]
                entry["prev_outlook"] = ol[0].strip() if len(ol) > 0 else ""
                entry["new_outlook"] = ol[2].strip() if len(ol) > 2 else ""
                i += 2
            else:
                i += 1

            results.append(entry)
        else:
            i += 1

    # 기업 단위 중복 제거 + 변동건만
    seen = set()
    changed = []
    for d in results:
        key = (d["company"], d["prev_rating"], d["new_rating"], d["prev_outlook"], d["new_outlook"])
        if key in seen: continue
        seen.add(key)
        if (d["prev_rating"] and d["new_rating"] and d["prev_rating"] != d["new_rating"]) or \
           (d["prev_outlook"] and d["new_outlook"] and d["prev_outlook"] != d["new_outlook"]):
            changed.append(d)

    print(f"  [한국신용평가] 전체 {len(results)}건 -> 변동 {len(changed)}건")
    return changed

# ============================================================
#  NICE신용평가 (HTML 테이블)
# ============================================================
def fetch_nice(session):
    print("  [NICE신용평가] 수집 중...")
    r = session.get("https://www.nicerating.com/disclosure/ratingChangeList.do", timeout=15)
    r.encoding = "utf-8"
    p = TableParser(); p.feed(r.text); rows = p.rows

    results = []
    for row in rows[2:]:
        if len(row) < 8: continue
        results.append({
            "source": "NICE신용평가",
            "company": row[0],
            "prev_rating": row[5],
            "new_rating": row[7] if len(row) > 7 else "",
            "prev_outlook": row[6] if len(row) > 6 else "",
            "new_outlook": row[8] if len(row) > 8 else "",
            "date": row[9] if len(row) > 9 else (row[-1] if len(row) > 8 else ""),
            "eval_type": row[4],
            "change_code": "",
        })

    seen = set()
    changed = []
    for d in results:
        key = (d["company"], d["prev_rating"], d["new_rating"], d["prev_outlook"], d["new_outlook"])
        if key in seen: continue
        seen.add(key)
        if d["prev_rating"] != d["new_rating"] or \
           (d["prev_outlook"] and d["new_outlook"] and d["prev_outlook"] != d["new_outlook"]):
            changed.append(d)

    print(f"  [NICE신용평가] 전체 {len(results)}건 -> 변동 {len(changed)}건")
    return changed

# ============================================================
#  통합 수집
# ============================================================
def scrape_all():
    session = requests.Session()
    session.headers.update(HEADERS)
    all_data = []
    for fetcher in [fetch_kr, fetch_kis, fetch_nice]:
        try:
            all_data.extend(fetcher(session))
        except Exception as e:
            print(f"  에러: {e}")
    print(f"\n  === 3사 합계: {len(all_data)}건 ===")
    return all_data

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

print("✅ 3사 통합 최종 코드 (v3) 로드 완료!")
print(f"   수집 기간: {WEEK_AGO} ~ {TODAY}")
print("   한국기업평가: 취소 건 제외")
print("   한국신용평가: 헤더/비등급 행 필터링")
print(f"📊 신용등급 모니터링 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print("="*60)
print("\n[1/3] 3사 데이터 수집...")
data = scrape_all()
print("\n[2/3] AI 분석...")
briefing = analyze(data) if data else "수집된 데이터가 없습니다."
print("\n[3/3] 알림 전송...")
send_tg(briefing)
print("\n✅ 완료!")