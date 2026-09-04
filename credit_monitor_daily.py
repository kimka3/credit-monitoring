"""신용등급 모니터링 — 한국기업평가 / 한국신용평가 / NICE신용평가 3사 통합 (v7)

매일 3사의 등급·전망 변동을 수집해 텔레그램으로 보낸다.

v6 -> v7 변경
  1. 실행 간 중복 제거. 조회 구간이 7~14일이라 같은 건이 매일 다시 잡히는데,
     기존 dedupe 는 한 번의 실행 안에서만 동작해서 같은 건이 반복 발송됐다.
     보낸 건의 키를 state/sent.json 에 남기고 신규만 보낸다.
  2. 모든 시각 계산을 한국시간으로. 러너가 UTC 라 datetime.now() 가 한국 기준
     전날 날짜를 냈다(브리핑 헤더 날짜와 조회 종료일이 하루씩 밀렸다).
  3. 파싱이 깨져도 "변동 없음"으로 보이던 문제. 페이지에서 행 자체를 못 뽑으면
     정상이 아니라 실패로 처리한다. 조용히 틀리는 것이 가장 위험하다.
  4. HTTP 상태 코드 확인 (raise_for_status).
  5. 텔레그램 전송 실패 시 비정상 종료. 기존에는 메시지가 안 갔는데도
     워크플로가 초록불이었다.
  6. 모델 claude-sonnet-4-20250514 -> claude-opus-5.
  7. 런타임 pip install 제거 (requirements.txt 로 이동).
  8. 등급표에 CCC+ / CCC- 추가. 없으면 해당 등급 건이 조용히 버려진다.
  9. 한국신용평가 리포트 PDF 를 텔레그램에 첨부. 회사명과 발행일이
     정확히 맞는 것만 붙인다. 한국기업평가는 회원 로그인이 필요하고
     NICE 는 목록에 PDF 링크 자체가 없어 대상이 아니다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --- 환경 ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 러너는 UTC 로 돌지만 대상은 한국 시장이다. 날짜 계산은 전부 KST 기준.
KST = ZoneInfo("Asia/Seoul")
NOW = datetime.now(KST)
TODAY = NOW.strftime("%Y-%m-%d")
WEEK_AGO = (NOW - timedelta(days=7)).strftime("%Y-%m-%d")
# NICE 는 간헐적으로 실패해서 14일을 훑는다. 중복은 state 로 걸러지므로
# 하루 실패해도 다음 날 자동으로 복구된다.
NICE_FROM = (NOW - timedelta(days=14)).strftime("%Y-%m-%d")

STATE_PATH = Path("state/sent.json")
STATE_KEEP_DAYS = 45          # 조회 구간(14일)보다 넉넉히
TIMEOUT = (30, 60)            # (연결, 읽기)
RETRIES = 3
RETRY_WAIT = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept": "text/html, application/json, */*",
}

RATING_GRADES = {
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
    "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-",
    "CCC+", "CCC", "CCC-", "CC", "C", "D",
    "A1", "A2+", "A2", "A2-", "A3+", "A3", "A3-",
}


KIS_LIST_URL = "https://www.kisrating.com/ratings/hot_disclosure.do"
KIS_DOWN_URL = "https://www.kisrating.com/fileDown.do"

# 목록 페이지의 PDF 버튼: fn_file(menuCd, gubun, 회사명, 파일명, 구분, 발행일)
KIS_PDF_RE = re.compile(
    r"fn_file\('([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*\.pdf)',\s*'([^']*)',\s*'(\d{8})'")

MAX_PDF_PER_ITEM = 3     # 한 회사·한 날짜에 회차별 리포트가 여럿 붙는 경우가 있다
MAX_PDF_PER_RUN = 10     # 텔레그램 도배 방지


class StructureError(RuntimeError):
    """페이지에서 행을 하나도 못 뽑은 경우.

    조용히 0건으로 넘기면 '변동 없음'이라는 좋은 소식처럼 보인다.
    사이트 개편이나 차단 페이지를 받은 것이므로 실패로 다뤄야 한다.
    """


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._in = [], [], "", False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in, self._cell = True, ""

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._row.append(self._cell.strip())
            self._in = False
        elif tag == "tr" and self._row:
            self.rows.append(self._row)

    def handle_data(self, data):
        if self._in:
            self._cell += data


def is_valid_rating(s: str) -> bool:
    """실제 신용등급인지. 'AAA (sf)', 'AA(보증)' 형태 지원."""
    if not s:
        return False
    return s.replace("(sf)", "").replace("보증", "").replace("(", "").replace(")", "").strip() in RATING_GRADES


def is_date(s: str) -> bool:
    return bool(re.match(r"\d{4}\.\d{2}\.\d{2}", s or ""))


def filter_changes(results):
    """등급·전망이 실제로 바뀐 건만. 실행 내 중복도 함께 제거한다."""
    seen, changed = set(), []
    for d in results:
        key = item_key(d)
        if key in seen:
            continue
        seen.add(key)
        if (d.get("change_code") and d["change_code"] not in ("", "0")) or \
           (d["prev_rating"] and d["new_rating"] and d["prev_rating"] != d["new_rating"]) or \
           (d["prev_outlook"] and d["new_outlook"] and d["prev_outlook"] != d["new_outlook"]):
            d["kind"] = change_kind(d)
            changed.append(d)
    return changed


# 등급 순서 (좋은 등급 -> 나쁜 등급). 상향·하향 판정에 쓴다.
LONG_ORDER = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
              "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-",
              "CCC+", "CCC", "CCC-", "CC", "C", "D"]
SHORT_ORDER = ["A1", "A2+", "A2", "A2-", "A3+", "A3", "A3-", "B+", "B", "B-", "C", "D"]
SHORT_ONLY = {"A1", "A2+", "A2", "A2-", "A3+", "A3", "A3-"}


def clean_rating(s: str) -> str:
    return (s or "").replace("(sf)", "").replace("보증", "").replace("(", "").replace(")", "").strip()


def rating_rank(s: str):
    """(체계, 순위). 장기·단기 등급은 서로 비교하지 않는다."""
    r = clean_rating(s)
    if r in SHORT_ONLY:
        return ("S", SHORT_ORDER.index(r))
    if r in LONG_ORDER:
        return ("L", LONG_ORDER.index(r))
    return None


def change_kind(d) -> str:
    """등급상향 / 등급하향 / 전망변경 / 신규평정 중 하나.

    등급은 그대로인데 전망만 바뀐 건이 많다. 그걸 'BBB+ -> BBB+' 로 보여주면
    무엇이 바뀐 건지 알 수 없다.
    """
    prev, new = d.get("prev_rating", ""), d.get("new_rating", "")
    if not prev:
        return "신규평정"
    if clean_rating(prev) != clean_rating(new):
        a, b = rating_rank(prev), rating_rank(new)
        if a and b and a[0] == b[0]:
            return "등급상향" if b[1] < a[1] else "등급하향"
        return "등급변경"
    if d.get("prev_outlook") and d.get("new_outlook") and d["prev_outlook"] != d["new_outlook"]:
        return "전망변경"
    return "변경"


def describe_change(d) -> str:
    """무엇이 바뀌었는지 한 줄로. 바뀐 축을 앞세운다."""
    kind = d.get("kind") or change_kind(d)
    prev, new = d.get("prev_rating", ""), d.get("new_rating", "")
    po, no = d.get("prev_outlook", ""), d.get("new_outlook", "")

    if kind == "전망변경":
        return f"{new} · 전망 {po} -> {no}"
    if kind == "신규평정":
        return f"신규 {new}" + (f" ({no})" if no else "")
    tail = ""
    if po or no:
        tail = f" (전망 {po or '-'} -> {no or '-'})" if po != no else f" ({no})"
    return f"{prev} -> {new}{tail}"


def item_key(d) -> str:
    """전송 이력 키. 날짜와 출처를 포함해야 서로 다른 건이 뭉개지지 않는다."""
    return "|".join([
        d.get("source", ""), d.get("company", ""), d.get("date", ""),
        d.get("prev_rating", ""), d.get("new_rating", ""),
        d.get("prev_outlook", ""), d.get("new_outlook", ""),
    ])


# --------------------------------------------------------------------------
#  전송 이력 (실행 간 중복 제거)
# --------------------------------------------------------------------------
def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        # 이력이 깨졌다고 알림을 멈출 수는 없다. 비우고 계속하되 알린다.
        print(f"  [warn] 전송 이력을 읽지 못해 새로 시작합니다: {type(exc).__name__}")
        return {}


def save_state(state: dict) -> None:
    cutoff = (NOW - timedelta(days=STATE_KEEP_DAYS)).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in state.items() if v >= cutoff}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(pruned, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"  전송 이력 {len(pruned)}건 저장 (만료 {len(state) - len(pruned)}건 정리)")


# --------------------------------------------------------------------------
#  한국기업평가 (AJAX API)
# --------------------------------------------------------------------------
def fetch_kr(session):
    print("  [한국기업평가] 수집 중...")
    r0 = session.get("https://www.korearatings.com/cms/frCmnCon/index.do?MENU_ID=360",
                     timeout=TIMEOUT)
    r0.raise_for_status()

    params = [
        ("MENU_ID", "360"), ("CONTENTS_NO", "1"), ("SITE_NO", "2"), ("COMP_CD", ""),
        ("STDT", WEEK_AGO), ("ENDT", TODAY),
    ] + [("SVCTY_CD", c) for c in
         ("01", "07", "02", "03", "10", "11", "05", "09", "04", "06", "08")]

    r = session.post(
        "https://www.korearatings.com/ajaxf/frDisclosureSvc/getRatingDisclosureList.do",
        headers={
            "Referer": "https://www.korearatings.com/cms/frCmnCon/index.do?MENU_ID=360",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        data=params, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    data = r.json()

    blocks = data.get("data")
    if not isinstance(blocks, dict) or not blocks:
        raise StructureError("응답에 data 블록이 없습니다 (API 변경 또는 차단)")

    all_items = []
    for val in blocks.values():
        if isinstance(val, dict) and "Data" in val:
            all_items.extend(val["Data"])

    results, n_cancel = [], 0
    for item in all_items:
        new_rating = (item.get("CUR_GRD_NM_ORG") or "").strip()
        if not new_rating:
            continue
        # 취소 건 제외 — 증권사 ELB/DLB 개별 회차 만기상환에 따른 등급 소멸이
        # 대부분이라 회사 등급 취소가 아니다. 전량 노이즈.
        if new_rating == "취소":
            n_cancel += 1
            continue
        results.append({
            "source": "한국기업평가",
            "company": (item.get("COMP_NM") or "").strip(),
            "prev_rating": (item.get("RBF_GRD_NM_ORG") or "").strip(),
            "new_rating": new_rating,
            "prev_outlook": (item.get("BFR_OL_NM") or "").strip(),
            "new_outlook": (item.get("OL_NM") or "").strip(),
            "date": item.get("EVAL_DT") or item.get("DSCLS_DTTM") or "",
            "eval_type": (item.get("EVAL_DIV_NM") or "").strip(),
            "change_code": item.get("GR_CHN_DVCD", "0"),
        })

    changed = filter_changes(results)
    print(f"  [한국기업평가] 원본 {len(all_items)}건 -> 취소 제외 {n_cancel}건 -> 변동 {len(changed)}건")
    return changed


# --------------------------------------------------------------------------
#  한국신용평가 (HTML) — 헤더 기반 컬럼 매핑
# --------------------------------------------------------------------------
def fetch_kis(session):
    print("  [한국신용평가] 수집 중...")
    r = session.get(KIS_LIST_URL, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    p = TableParser()
    p.feed(r.text)
    if not p.rows:
        raise StructureError("표 행을 하나도 찾지 못했습니다 (페이지 구조 변경 또는 차단)")

    results, colmap, last_entry = [], None, None
    for row in p.rows:
        cells = [c.strip() for c in row]

        if "회사명" in cells:
            colmap = {}
            for i, c in enumerate(cells):
                if c == "회사명":
                    colmap["company"] = i
                elif c in ("직전등급", "직전"):
                    colmap["prev"] = i
                elif c in ("현재등급", "현재"):
                    colmap["cur"] = i
                elif c == "평가일":
                    colmap["date"] = i
                elif c == "평가종류":
                    colmap["type"] = i
            last_entry = None
            continue

        if len(cells) <= 4:
            # 전망은 다음 줄에 따로 오는 구조
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
            "source": "한국신용평가", "company": company,
            "prev_rating": prev_r if is_valid_rating(prev_r) else "",
            "new_rating": new_r, "prev_outlook": "", "new_outlook": "",
            "date": date,
            "eval_type": cells[colmap["type"]] if "type" in colmap else "",
            "change_code": "",
        }
        results.append(entry)
        last_entry = entry

    if colmap is None:
        raise StructureError("'회사명' 헤더를 찾지 못했습니다 (표 구조 변경)")

    # 같은 페이지에 리포트 PDF 버튼이 함께 있다. 회사명과 발행일이 정확히
    # 맞는 것만 붙인다. 최근 것을 아무거나 붙이면 이번 변동과 무관한 리포트가
    # 딸려가므로, 애매하면 안 붙이는 편이 낫다.
    # 한 회사·한 날짜에 회차만 다른 리포트가 여럿 걸린다. 실측하면 내용이
    # 사실상 같은 문서라(96바이트 차이) 여러 개를 보내면 같은 PDF 가 반복해서
    # 온다. 문서 종류(Rating Summary 등)별로 하나씩만 남긴다.
    pdf_map = {}
    for menu_cd, gubun, title, fname, kind, wdate in KIS_PDF_RE.findall(r.text):
        bucket = pdf_map.setdefault((title.strip(), wdate), {})
        bucket.setdefault(kind.strip(), {
            "menuCd": menu_cd, "gubun": gubun, "title": title.strip(),
            "file": fname, "writedate": wdate, "kind": kind.strip()})
    for e in results:
        by_kind = pdf_map.get((e["company"].strip(), e["date"].replace(".", "")), {})
        e["pdfs"] = list(by_kind.values())[:MAX_PDF_PER_ITEM]

    changed = filter_changes(results)
    n_pdf = sum(len(c.get("pdfs") or []) for c in changed)
    print(f"  [한국신용평가] 원본 {len(results)}건 -> 변동 {len(changed)}건, 리포트 {n_pdf}개 매칭")
    return changed


# --------------------------------------------------------------------------
#  NICE신용평가 (HTML) — 14일 조회
#  파라미터 없이 호출하면 항상 빈 결과. ratingGubn: R=등급, O=전망, RO=둘다
# --------------------------------------------------------------------------
def fetch_nice(session):
    print("  [NICE신용평가] 수집 중...")
    r = session.get(
        "https://www.nicerating.com/disclosure/ratingChangeList.do",
        params=[("today", TODAY), ("cmpCd", ""),
                ("strDate", NICE_FROM), ("endDate", TODAY),
                ("ratingGubn", "RO"), ("searchType", "0")],
        timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    p = TableParser()
    p.feed(r.text)
    if not p.rows:
        raise StructureError("표 행을 하나도 찾지 못했습니다 (페이지 구조 변경 또는 차단)")

    results, n_unknown = [], 0
    for row in p.rows:
        cells = [c.strip() for c in row]
        if "기업명" in cells or cells[:2] == ["등급", "전망"]:
            continue
        if not cells or not is_date(cells[-1]):
            continue

        n = len(cells)
        if n == 10:
            # 채권: 기업명,회차,상환순위,종류,평정,직전등급,직전전망,현재등급,현재전망,확정일
            e = dict(company=cells[0], eval_type=cells[4],
                     prev_rating=cells[5], prev_outlook=cells[6],
                     new_rating=cells[7], new_outlook=cells[8], date=cells[9])
        elif n == 7:
            e = dict(company=cells[0], eval_type=cells[1],
                     prev_rating=cells[2], prev_outlook=cells[3],
                     new_rating=cells[4], new_outlook=cells[5], date=cells[6])
        elif n == 5:
            e = dict(company=cells[0], eval_type=cells[1],
                     prev_rating=cells[2], prev_outlook="",
                     new_rating=cells[3], new_outlook="", date=cells[4])
        else:
            n_unknown += 1
            continue

        if not is_valid_rating(e["new_rating"]):
            continue
        if e["prev_rating"] and not is_valid_rating(e["prev_rating"]):
            e["prev_rating"] = ""
        e.update(source="NICE신용평가", change_code="")
        results.append(e)

    # 날짜로 끝나는 행은 있는데 컬럼 수가 전부 낯설다면 표가 바뀐 것이다.
    if not results and n_unknown:
        raise StructureError(f"컬럼 수가 예상과 다릅니다 ({n_unknown}행, 표 구조 변경)")

    changed = filter_changes(results)
    print(f"  [NICE신용평가] 원본 {len(results)}건 -> 변동 {len(changed)}건 ({NICE_FROM}~{TODAY})")
    return changed


# --------------------------------------------------------------------------
#  수집 — 3회 재시도
# --------------------------------------------------------------------------
def scrape_all():
    session = requests.Session()
    session.headers.update(HEADERS)
    all_data, errors = [], []

    for fetcher in (fetch_kr, fetch_kis, fetch_nice):
        last_err = None
        for attempt in range(RETRIES):
            try:
                all_data.extend(fetcher(session))
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                print(f"  재시도 {attempt + 1}/{RETRIES} [{fetcher.__name__}]: "
                      f"{type(exc).__name__}: {exc}")
                if attempt < RETRIES - 1:
                    time.sleep(RETRY_WAIT)
        if last_err:
            print(f"  최종 실패 [{fetcher.__name__}]: {type(last_err).__name__}: {last_err}")
            errors.append(f"{fetcher.__name__} ({type(last_err).__name__})")

    print(f"\n  === 3사 합계: 변동 {len(all_data)}건, 실패 {len(errors)}곳 ===")
    # 리포트를 같은 세션으로 받아야 쿠키가 유지된다.
    return all_data, errors, session


# --------------------------------------------------------------------------
#  Claude 분석
# --------------------------------------------------------------------------
SYSTEM = """당신은 한국 채권시장 전문 애널리스트입니다.
증권사 신탁부서에서 특정금전신탁 포트폴리오를 운용하는 팀장에게
매일 아침 신용등급 변동 브리핑을 제공합니다.
등급 하향 및 부도(D) 건은 ⚠️로 최우선 표시. 투자적격등급(BBB-이상) 중심.
금융채와 일반 회사채 구분. 같은 기업 개별 회차는 1줄로 통합.
동일 계열/시리즈에서 복수 건이 발생하면 패턴으로 묶어 언급.
간결한 한국어, 최대 2000자."""


def analyze(changes):
    if not ANTHROPIC_API_KEY:
        print("  ANTHROPIC_API_KEY 없음 — 원문 형식으로 보냅니다.")
        return format_plain(changes)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        r = client.messages.create(
            model="claude-opus-5",
            max_tokens=8000,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            messages=[{"role": "user", "content":
                       f"오늘: {TODAY}\n\n신규 등급변동 데이터:\n"
                       f"{json.dumps(changes, ensure_ascii=False, indent=2)}\n\n"
                       "각 건의 kind 필드가 변동 유형이다(등급상향/등급하향/"
                       "전망변경/신규평정). 이 분류를 그대로 따르고 임의로 바꾸지 말 것.\n\n"
                       "형식:\n📊 [날짜] 신용등급 변동 브리핑\n"
                       "■ 부도/등급 하향 (⚠️)\n  - 업체명 | 변경전→변경후 | 출처\n"
                       "■ 등급 상향\n  - 업체명 | 변경전→변경후 | 출처\n"
                       "■ 전망(Outlook) 변경\n"
                       "  - 업체명 | 등급 유지 · 전망 변경전→변경후 | 출처\n"
                       "    (등급은 그대로이므로 'BBB+ → BBB+' 처럼 쓰지 말 것)\n"
                       "■ 신규 평정\n■ 신탁 포트폴리오 시사점 (2-3줄)\n"
                       "빈 카테고리는 생략. 2000자 이내."}])
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        return text or format_plain(changes)
    except Exception as exc:
        # AI 요약이 실패해도 원문은 보낸다. 알림 자체를 거르면 안 된다.
        print(f"  Claude 실패 ({type(exc).__name__}: {exc}) — 원문 형식으로 대체")
        return format_plain(changes)


ORDER = {"등급하향": 0, "등급변경": 1, "등급상향": 2, "전망변경": 3, "신규평정": 4, "변경": 5}


def format_plain(changes):
    lines = [f"📊 [{TODAY}] 신용등급 변동 내역 ({len(changes)}건)\n"]
    for c in sorted(changes, key=lambda c: (0 if c.get("new_rating") == "D" else 1,
                                            ORDER.get(c.get("kind"), 9))):
        kind = c.get("kind") or change_kind(c)
        mark = "⚠️ " if c.get("new_rating") == "D" or kind == "등급하향" else ""
        lines.append(f"- {mark}[{kind}] {c['company']} | {describe_change(c)} | "
                     f"{c['source']} [{c.get('date', '')}]")
    return "\n".join(lines)


# --------------------------------------------------------------------------
#  텔레그램
# --------------------------------------------------------------------------
def send_tg(msg: str) -> None:
    """실패하면 예외를 올린다. 조용히 넘기면 메시지가 안 갔는데도 초록불이 된다."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n" + "=" * 60 + f"\n{msg}\n" + "=" * 60)
        print("  (텔레그램 시크릿이 없어 화면 출력으로 대체)")
        return

    for i in range(0, len(msg), 4000):
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg[i:i + 4000],
                  "disable_web_page_preview": True},
            timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"텔레그램 전송 실패 (HTTP {r.status_code}): {r.text[:300]}")
    print("  텔레그램 전송 완료")


def download_kis_pdf(session, meta) -> bytes:
    r = session.post(KIS_DOWN_URL,
                     data={"fileName": meta["file"], "fileTitle": meta["title"],
                           "menuCd": meta["menuCd"], "gubun": meta["gubun"],
                           "writedate": meta["writedate"], "freeYn": ""},
                     headers={"Referer": KIS_LIST_URL}, timeout=(30, 120))
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        # 유료·로그인 전환 시 HTML 이 돌아온다. 그대로 첨부하면 안 된다.
        raise RuntimeError(f"PDF 가 아닙니다 ({len(r.content)}바이트)")
    return r.content


def send_tg_document(content: bytes, filename: str, caption: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"    (시크릿 없음 — {filename} 전송 생략)")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
        data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000]},
        files={"document": (filename, content, "application/pdf")}, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")


def send_reports(session, changes) -> int:
    """등급변동 건에 매칭된 한국신용평가 리포트를 이어서 보낸다.

    브리핑은 이미 나갔으므로 여기서 실패해도 전체를 실패로 만들지 않는다.
    리포트를 못 받은 것과 브리핑이 안 간 것은 심각도가 다르다.
    한국기업평가는 회원 로그인이 필요하고 NICE 는 목록에 링크가 없어
    한국신용평가 건만 대상이 된다.
    """
    jobs = [(c, p) for c in changes for p in (c.get("pdfs") or [])][:MAX_PDF_PER_RUN]
    if not jobs:
        print("  첨부할 리포트 없음")
        return 0

    sent = failed = skipped = 0
    seen = set()
    for c, meta in jobs:
        try:
            content = download_kis_pdf(session, meta)
            # 같은 회사·같은 날짜에 회차만 다른 리포트가 내용은 동일한 경우가
            # 잦다(실측: 3건 중 2건이 바이트 단위로 동일). 같은 파일을 여러 번
            # 보내면 채팅창만 지저분해진다.
            digest = hashlib.sha256(content).hexdigest()
            if digest in seen:
                skipped += 1
                print(f"    건너뜀 {meta['file']} (앞서 보낸 것과 내용 동일)")
                continue
            seen.add(digest)

            prev = c.get("prev_rating") or "신규"
            caption = (f"{c['company']} | {prev} -> {c['new_rating']} | {c['source']}"
                       f"\n{c.get('eval_type', '')} {c.get('date', '')}".rstrip())
            send_tg_document(content, meta["file"], caption)
            sent += 1
            print(f"    보냄 {c['company']} {meta['file']} ({len(content):,}B)")
        except Exception as exc:
            failed += 1
            print(f"    실패 {c['company']} {meta['file']}: {type(exc).__name__}: {exc}")
    print(f"  리포트 {sent}건 전송"
          + (f", 중복 {skipped}건 제외" if skipped else "")
          + (f", {failed}건 실패" if failed else ""))
    return sent


# --------------------------------------------------------------------------
def main() -> int:
    print(f"📊 신용등급 모니터링 v7 — {NOW:%Y-%m-%d %H:%M} KST")
    print(f"   조회: {WEEK_AGO} ~ {TODAY} (NICE: {NICE_FROM} ~ {TODAY})")
    print("=" * 60)

    print("\n[1/5] 3사 수집...")
    data, errors, session = scrape_all()

    print("\n[2/5] 기존 발송분 제외...")
    state = load_state()
    fresh = [d for d in data if item_key(d) not in state]
    print(f"  변동 {len(data)}건 중 신규 {len(fresh)}건 (기발송 {len(data) - len(fresh)}건 제외)")

    print("\n[3/5] 브리핑 작성...")
    if errors and not data:
        # 전부 실패했는데 "변동 없음"이라고 하면 좋은 소식으로 오해한다.
        briefing = (f"⚠️ [{TODAY}] 신용등급 수집 실패\n\n"
                    f"3사 모두 데이터를 가져오지 못했습니다.\n"
                    f"실패: {', '.join(errors)}\n\n"
                    f"등급 변동이 없다는 뜻이 아닙니다. 사이트 구조 변경 여부를 확인하세요.")
    elif not fresh:
        briefing = f"📊 [{TODAY}] 신용등급 변동 브리핑\n\n✅ 신규 등급변동 내역이 없습니다."
    else:
        briefing = analyze(fresh)

    if errors and data:
        briefing += "\n\n⚠️ 일부 수집 실패: " + ", ".join(errors)

    print("\n[4/5] 브리핑 전송...")
    send_tg(briefing)

    print("\n[5/5] 리포트 첨부...")
    send_reports(session, fresh)

    # 전송에 성공한 건만 이력에 남긴다. 먼저 저장하면 전송 실패 시 영영 누락된다.
    if fresh:
        for d in fresh:
            state[item_key(d)] = TODAY
        save_state(state)

    if errors:
        print(f"\n⚠️ 완료 (일부 수집 실패: {', '.join(errors)})")
        return 1
    print("\n✅ 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
