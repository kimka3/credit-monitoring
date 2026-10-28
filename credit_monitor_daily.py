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
  9. 3사 리포트 PDF 를 텔레그램에 첨부. 회사명과 발행일이 정확히 맞는
     것만 붙인다. 한국기업평가는 평가의견(신용평가요지)까지 무료이고
     전체 평가리포트만 유료다. NICE 는 목록이 아니라 기업 상세 페이지에
     의견서가 있다.
 10. 휴대폰용 웹 리포트. state/history.json 에 변동을 누적하고 report.html 을
     만들어 GitHub Pages 로 배포한다. 텔레그램은 새 소식을 알리는 통보이고,
     이쪽은 지금까지 무슨 일이 있었는지 되돌아보는 화면이다.
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
from monitor_checks import (IntegrityError, read_json, write_json, json_tables, validate_html_count,
    validate_kis_count, verify_selection, verify_persisted, pending_alerts, update_health, volume_issues)

# --- 환경 ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 러너는 UTC 로 돌지만 대상은 한국 시장이다. 날짜 계산은 전부 KST 기준.
KST = ZoneInfo("Asia/Seoul")
NOW = datetime.now(KST)
TODAY = NOW.strftime("%Y-%m-%d")
WEEK_AGO = (NOW - timedelta(days=30)).strftime("%Y-%m-%d")
# 최근 30일을 재조회해 지연 공시와 일시적인 수집 누락을 복구한다.
NICE_FROM = WEEK_AGO
WATCH_FROM = (NOW - timedelta(days=30)).strftime("%Y.%m.%d")

STATE_PATH = Path("state/sent.json")
STATE_KEEP_DAYS = 45          # 조회 구간(30일)보다 넉넉히
HISTORY_PATH = Path("state/history.json")
HEALTH_PATH = Path("state/health.json")
SOURCE_STATS = {}
HISTORY_KEEP_DAYS = 180       # 웹 리포트에 쌓아 보여줄 기간
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
# WR은 상환 등에 따른 등급 취소다. 등급 상하향과 구분해 원본 건수에만 집계한다.
NON_RATING_STATUSES = {"취소", "WR", "NR", "N/R", "-", "—"}


KIS_LIST_URL = "https://www.kisrating.com/ratings/hot_disclosure.do"
KIS_DOWN_URL = "https://www.kisrating.com/fileDown.do"

# 목록 페이지의 PDF 버튼: fn_file(menuCd, gubun, 회사명, 파일명, 구분, 발행일)
KIS_PDF_RE = re.compile(
    r"fn_file\('([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*\.pdf)',\s*'([^']*)',\s*'(\d{8})'")

MAX_PDF_PER_ITEM = 3     # 한 회사·한 날짜에 회차별 리포트가 여럿 붙는 경우가 있다
MAX_PDF_PER_RUN = 10     # 텔레그램 도배 방지

KR_LIST_URL = "https://www.korearatings.com/cms/frCmnCon/index.do?MENU_ID=360"
KR_DOWN_URL = "https://www.korearatings.com/ajaxa/fileCpnt/reportFileDown.do"

NICE_LIST_URL = "https://www.nicerating.com/disclosure/ratingChangeList.do"
NICE_COMP_URL = "https://www.nicerating.com/disclosure/companyGradeInfo.do"
NICE_DOWN_URL = "https://www.nicerating.com/common/fileDown.do"
NICE_WATCH_URL = "https://www.nicerating.com/disclosure/totalWatchList.do"
KR_WATCH_URL = "https://www.korearatings.com/ajaxf/frDisclosureSvc/getWatchList.do"
RECORD_FIELDS = ("source", "company", "date", "prev_rating", "new_rating",
                 "prev_outlook", "new_outlook", "kind", "eval_type",
                 "prev_watch", "new_watch", "watch_reason", "instrument",
                 "event_type", "source_url")

# 목록 행의 회사명 링크: goView('CORP', '5207805') — 상세 페이지 키를 여기서만 얻는다.
NICE_CMP_RE = re.compile(r"goView\(\s*'\w+'\s*,\s*'(\d+)'\s*\)\"\s*>([^<]+)</a>")

# 상세 페이지의 파일 버튼: fncFileDown('UUID') 뒤 img 의 alt 로 종류가 갈린다.
# ico_file01_1 = 의견서, ico_file04 = 재무. 등급 근거가 담긴 의견서만 첨부한다.
NICE_PDF_RE = re.compile(
    r"fncFileDown\('([0-9a-fA-F-]{36})'\)\"?\s*>\s*<img[^>]*alt=\"([^\"]*)\"")
NICE_PDF_KIND = "의견서"


class StructureError(IntegrityError):
    """페이지에서 행을 하나도 못 뽑은 경우.

    조용히 0건으로 넘기면 '변동 없음'이라는 좋은 소식처럼 보인다.
    사이트 개편이나 차단 페이지를 받은 것이므로 실패로 다뤄야 한다.
    """


class TableParser(HTMLParser):
    def __init__(self, expand_colspans=False):
        super().__init__()
        self.rows, self._row, self._cell, self._in = [], [], "", False
        self.expand_colspans = expand_colspans
        self._colspan = 1
        self.row_links, self._links = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
            self._links = []
        elif tag in ("td", "th"):
            self._in, self._cell = True, ""
            self._colspan = int(dict(attrs).get("colspan") or 1) if self.expand_colspans else 1
        elif tag == "a":
            self._links.append(dict(attrs).get("href") or "")

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._row.append(self._cell.strip())
            self._row.extend([""] * (self._colspan - 1))
            self._in = False
        elif tag == "tr" and self._row:
            self.rows.append(self._row)
            self.row_links.append(self._links)

    def handle_data(self, data):
        if self._in:
            self._cell += data


def is_valid_rating(s: str) -> bool:
    """실제 신용등급인지. 'AAA (sf)', 'AA(보증)', 'CCC↓' 형태 지원."""
    return bool(s) and clean_rating(s) in RATING_GRADES


def is_date(s: str) -> bool:
    return bool(re.match(r"\d{4}\.\d{2}\.\d{2}", s or ""))


def checked_changes(rows, label):
    selected = filter_changes(rows)
    verify_selection(rows, selected, item_key, label)
    SOURCE_STATS[label] = {"parsed": len(rows), "changes": len(selected)}
    SOURCE_STATS[label]["latest_date"] = max((r.get("date", "") for r in rows), default="")
    return selected


def valid_previous(value, label):
    if not value or value in NON_RATING_STATUSES:
        return ""
    if not is_valid_rating(value):
        raise StructureError(f"{label}: 알 수 없는 직전등급 {value!r}")
    return value


def filter_changes(results):
    """등급·전망이 실제로 바뀐 건만. 실행 내 중복도 함께 제거한다."""
    seen, changed = set(), []
    for d in results:
        key = item_key(d)
        if key in seen:
            continue
        seen.add(key)
        if d.get("event_type") == "watch_registration" or \
           ("prev_watch" in d and d.get("prev_watch", "") != d.get("new_watch", "")) or \
           (d.get("change_code") and str(d["change_code"]) not in ("", "0")) or \
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


# 평가사가 등급 뒤에 붙이는 Rating Watch 표시. NICE 는 '↓', 한국기업평가는 '↑'.
# 등급 자체가 아니라 '검토 중'이라는 딱지이므로 판정·비교에서는 떼어낸다.
# 화면에는 그대로 남긴다 — 'CCC↓ -> D' 가 'CCC -> D' 보다 많은 것을 말해준다.
WATCH_MARKS = "↑↓↕"


def clean_rating(s: str) -> str:
    s = (s or "").replace("(sf)", "").replace("보증", "").replace("(", "").replace(")", "")
    return s.strip().strip(WATCH_MARKS).strip()


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
    if d.get("event_type") == "watch_registration":
        return "검토변경"
    if not prev:
        return "신규평정"
    if clean_rating(prev) != clean_rating(new):
        a, b = rating_rank(prev), rating_rank(new)
        if a and b and a[0] == b[0]:
            return "등급상향" if b[1] < a[1] else "등급하향"
        return "등급변경"
    if d.get("prev_watch", "") != d.get("new_watch", ""):
        return "검토변경"
    if d.get("prev_outlook") and d.get("new_outlook") and d["prev_outlook"] != d["new_outlook"]:
        return "전망변경"
    if prev != new:
        # 등급은 같고 Rating Watch 딱지만 붙거나 떨어진 경우 ('CCC↓' -> 'CCC').
        return "검토변경"
    return "변경"


def describe_change(d) -> str:
    """무엇이 바뀌었는지 한 줄로. 바뀐 축을 앞세운다."""
    kind = d.get("kind") or change_kind(d)
    prev, new = d.get("prev_rating", ""), d.get("new_rating", "")
    po, no = d.get("prev_outlook", ""), d.get("new_outlook", "")
    if d.get("new_watch"):
        grade = new if not prev or clean_rating(prev) == clean_rating(new) else f"{prev} -> {new}"
        text = f"{grade} · {d['new_watch']} 등록"
        if d.get("instrument"):
            text += f" ({d['instrument']})"
        if d.get("watch_reason"):
            text += f" · {d['watch_reason']}"
        return text

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
    key = "|".join([
        d.get("source", ""), d.get("company", ""), d.get("date", ""),
        d.get("prev_rating", ""), d.get("new_rating", ""),
        d.get("prev_outlook", ""), d.get("new_outlook", ""),
    ])
    if d.get("event_type") == "watch_registration":
        key += "|watch|" + d.get("instrument", "")
    elif d.get("instrument") in ("SF", "유동화익스포져"):
        key += "|instrument:" + d["instrument"]
    return key


def notification_key(d):
    # 기존 한기평 이력을 덮어 보완하되, 누락됐던 검토 상태는 한 번 알린다.
    key = item_key(d)
    if d.get("new_watch"):
        key += "|watch:" + d["new_watch"]
    return key


def watch_name(value):
    value = (value or "").strip()
    return {"00": "", "0": "", "01": "상향검토", "02": "하향검토", "03": "미확정검토",
            "↑": "상향검토", "↓": "하향검토", "↕": "미확정검토",
            "긍정적 검토": "상향검토", "부정적 검토": "하향검토"}.get(value, value or "")


# --------------------------------------------------------------------------
#  전송 이력 (실행 간 중복 제거)
# --------------------------------------------------------------------------
def load_state() -> dict:
    return read_json(STATE_PATH, {})


def load_history() -> dict:
    """웹 리포트용 변동 이력. 전송 이력(sent.json)과 목적이 다르다.

    sent.json 은 '이미 보냈나'만 알면 되므로 45일 뒤 지운다. 이쪽은 화면에
    보여줄 내용이라 더 오래, 더 자세히 남긴다.
    """
    if HISTORY_PATH.exists():
        return read_json(HISTORY_PATH, {})

    # 첫 실행: 전송 이력의 키를 되살려 빈 화면으로 시작하지 않게 한다.
    seeded = {}
    for key, sent_on in load_state().items():
        parts = key.split("|")
        if len(parts) != 7:
            continue
        src, comp, date, pr, nr, po, no = parts
        rec = dict(source=src, company=comp, date=date, prev_rating=pr,
                   new_rating=nr, prev_outlook=po, new_outlook=no,
                   eval_type="", first_seen=sent_on)
        rec["kind"] = change_kind(rec)
        seeded[key] = rec
    if seeded:
        print(f"  이력 없음 — 전송 기록 {len(seeded)}건으로 시작합니다.")
    return seeded


def save_history(hist: dict) -> None:
    cutoff = (NOW - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in hist.items() if (v.get("first_seen") or "") >= cutoff}
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(HISTORY_PATH, pruned)
    print(f"  변동 이력 {len(pruned)}건 저장 (만료 {len(hist) - len(pruned)}건 정리)")


def save_state(state: dict) -> None:
    cutoff = (NOW - timedelta(days=STATE_KEEP_DAYS)).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in state.items() if v >= cutoff}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(STATE_PATH, pruned)
    print(f"  전송 이력 {len(pruned)}건 저장 (만료 {len(state) - len(pruned)}건 정리)")


# --------------------------------------------------------------------------
#  한국기업평가 (AJAX API)
# --------------------------------------------------------------------------
def fetch_kr(session):
    print("  [한국기업평가] 수집 중...")
    r0 = session.get(KR_LIST_URL, timeout=TIMEOUT)
    r0.raise_for_status()

    params = [
        ("MENU_ID", "360"), ("CONTENTS_NO", "1"), ("SITE_NO", "2"), ("COMP_CD", ""),
        ("STDT", WEEK_AGO), ("ENDT", TODAY),
    ] + [("SVCTY_CD", c) for c in
         ("01", "07", "02", "03", "10", "11", "05", "09", "04", "06", "08")]

    r = session.post(
        "https://www.korearatings.com/ajaxf/frDisclosureSvc/getRatingDisclosureList.do",
        headers={
            "Referer": KR_LIST_URL,
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

    all_items = json_tables(blocks, "한국기업평가", (
        "data31", "data32", "data33", "data34", "data35", "data36",
        "data38", "data39", "data3A", "data3B", "data3C"))

    results, n_cancel = [], 0
    for item in all_items:
        new_rating = (item.get("CUR_GRD_NM_ORG") or "").strip()
        if not new_rating:
            raise StructureError("한국기업평가: 현재등급 필드 누락")
        # 취소 건 제외 — 증권사 ELB/DLB 개별 회차 만기상환에 따른 등급 소멸이
        # 대부분이라 회사 등급 취소가 아니다. 전량 노이즈.
        if new_rating == "취소":
            n_cancel += 1
            continue
        if not is_valid_rating(new_rating) or not item.get("COMP_NM") or not is_date(item.get("EVAL_DT") or item.get("DSCLS_DTTM") or ""):
            raise StructureError("한국기업평가: 회사/등급/평가일 필드 변경")
        results.append({
            "source": "한국기업평가",
            "company": (item.get("COMP_NM") or "").strip(),
            "prev_rating": valid_previous((item.get("RBF_GRD_NM_ORG") or "").strip(), "한국기업평가"),
            "new_rating": new_rating,
            "prev_outlook": (item.get("BFR_OL_NM") or "").strip(),
            "new_outlook": (item.get("OL_NM") or "").strip(),
            "date": item.get("EVAL_DT") or item.get("DSCLS_DTTM") or "",
            "eval_type": (item.get("EVAL_DIV_NM") or "").strip(),
            "change_code": item.get("GR_CHN_DVCD", "0"),
            "new_watch": watch_name(item.get("CRAL_NM") or item.get("CUR_CRAL_CD") or ""),
            # 평가의견(신용평가요지) 다운로드에 필요한 값들
            "opn_file": (item.get("RTNG_OPN_FILE_NM") or "").strip(),
            "svc_id": (item.get("SVC_ID") or "").strip(),
            "eval_seq": item.get("EVAL_SEQNO"),
            "comp_cd": (item.get("COMP_CD") or "").strip(),
        })

    changed = checked_changes(results, "한국기업평가")
    SOURCE_STATS["한국기업평가"].update(raw=len(all_items), excluded_cancel=n_cancel)
    attach_kr_pdfs(changed)
    n_pdf = sum(len(c.get("pdfs") or []) for c in changed)
    print(f"  [한국기업평가] 원본 {len(all_items)}건 -> 취소 제외 {n_cancel}건 "
          f"-> 변동 {len(changed)}건, 리포트 {n_pdf}개 매칭")
    return changed


def attach_kr_pdfs(changes) -> None:
    """평가의견 파일을 변동 건에 붙인다. 목록 응답에 값이 다 들어 있어 추가 요청이 없다.

    유료인 것은 '평가리포트'(RPT_FILE_NM) 쪽이고, 평가의견(RTNG_OPN_FILE_NM,
    rptNo=03)은 로그인 없이 받아진다. 권한 확인 페이지가 유료 안내문을 늘
    함께 내려주지만 실제로는 곧바로 다운로드를 실행한다.
    """
    for e in changes:
        e.setdefault("pdfs", [])
        if not (e.get("opn_file") and e.get("svc_id") and e.get("eval_seq")):
            continue
        e["pdfs"].append({
            "src": "kr", "file": f"{e['company']}_{e.get('date', '')}.pdf",
            "kind": "평가의견", "encFileNm": e["opn_file"], "encSvcSeqNo": e["svc_id"],
            "evalNo": str(e["eval_seq"]), "compCd": e.get("comp_cd", ""),
        })


# --------------------------------------------------------------------------
#  한국신용평가 (HTML) — 헤더 기반 컬럼 매핑
# --------------------------------------------------------------------------
def fetch_kis(session):
    print("  [한국신용평가] 수집 중...")
    r = session.post(KIS_LIST_URL, data={"tabType": "0", "searchYn": "Y",
        "startDt": WEEK_AGO.replace("-", "."), "endDt": TODAY.replace("-", ".")},
        timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    # Issuer Rating/보험금지급능력평가 헤더는 직전·현재가 각각
    # 등급과 Outlook 두 열을 차지한다. colspan을 펼쳐 실제 데이터 열에 맞춘다.
    p = TableParser(expand_colspans=True)
    p.feed(r.text)
    if not p.rows:
        raise StructureError("표 행을 하나도 찾지 못했습니다 (페이지 구조 변경 또는 차단)")

    results, colmap, last_entry, raw_count, n_cancel = [], None, None, 0, 0
    for row, links in zip(p.rows, p.row_links):
        cells = [c.strip() for c in row]
        if any(is_date(c) for c in cells):
            raw_count += 1

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
            if "기준통화" in cells and "prev" in colmap and "cur" in colmap:
                colmap["prev_outlook"] = colmap["prev"] + 1
                colmap["new_outlook"] = colmap["cur"] + 1
            last_entry = None
            continue

        if len(cells) <= 4:
            if any(is_date(c) for c in cells):
                raise StructureError("한국신용평가: 날짜가 있는 불완전한 행")
            # 전망은 다음 줄에 따로 오는 구조
            if last_entry and len(cells) >= 3:
                last_entry["prev_outlook"] = cells[0]
                last_entry["new_outlook"] = cells[2]
            continue

        dated = any(is_date(c) for c in cells)
        if not colmap or "cur" not in colmap or "company" not in colmap:
            if dated:
                raise StructureError("한국신용평가: 날짜가 있는 행의 등급 헤더 누락")
            continue
        if max(colmap.values()) >= len(cells):
            if dated:
                raise StructureError("한국신용평가: 일부 데이터 행의 열 수 변경")
            continue

        company = cells[colmap["company"]]
        new_r = cells[colmap["cur"]]
        prev_r = cells[colmap["prev"]] if "prev" in colmap else ""
        date = cells[colmap["date"]] if "date" in colmap else ""

        if company and any(is_date(c) for c in cells) and not is_date(date):
            raise StructureError(f"평가일 열이 맞지 않습니다 ({company}, 표 구조 변경)")

        if dated and new_r in NON_RATING_STATUSES:
            n_cancel += 1
            last_entry = None
            continue
        if dated and (not company or not is_valid_rating(new_r)):
            raise StructureError(f"한국신용평가: 회사/등급 필드 변경 ({company}: {new_r!r})")
        if not company or not is_valid_rating(new_r) or not is_date(date):
            last_entry = None
            continue

        entry = {
            "source": "한국신용평가", "company": company,
            "prev_rating": valid_previous(prev_r, "한국신용평가"),
            "new_rating": new_r,
            "prev_outlook": cells[colmap["prev_outlook"]] if "prev_outlook" in colmap else "",
            "new_outlook": cells[colmap["new_outlook"]] if "new_outlook" in colmap else "",
            "date": date,
            "eval_type": cells[colmap["type"]] if "type" in colmap else "",
            "change_code": "",
            "pdfs": [],
        }
        # 같은 회사·날짜에도 CP/단기사채/Issuer 의견서는 서로 다르다.
        # 이번 평가 행에 연결된 문서만 사용한다.
        by_kind = {}
        for href in links:
            for menu_cd, gubun, title, fname, kind, wdate in KIS_PDF_RE.findall(href):
                if title.strip() == company and wdate == date.replace(".", ""):
                    by_kind.setdefault(kind.strip(), {
                        "menuCd": menu_cd, "gubun": gubun, "title": title.strip(),
                        "file": fname, "writedate": wdate, "kind": kind.strip()})
        entry["pdfs"] = list(by_kind.values())[:MAX_PDF_PER_ITEM]
        results.append(entry)
        last_entry = entry

    if colmap is None:
        raise StructureError("'회사명' 헤더를 찾지 못했습니다 (표 구조 변경)")
    if not raw_count and not re.search(r"(조회|검색|해당|평가).{0,25}(없습니다|없음)", r.text):
        raise StructureError("한국신용평가: 0건을 확인할 명시적 원본 표시가 없습니다")

    validate_kis_count(r.text, raw_count)
    changed = checked_changes(results, "한국신용평가")
    SOURCE_STATS["한국신용평가"].update(raw=raw_count, excluded_cancel=n_cancel)
    n_pdf = sum(len(c.get("pdfs") or []) for c in changed)
    print(f"  [한국신용평가] 원본 {raw_count}건 -> 취소/미평가 제외 {n_cancel}건 -> 변동 {len(changed)}건, 리포트 {n_pdf}개 매칭")
    return changed


# --------------------------------------------------------------------------
#  NICE신용평가 (HTML) — 30일 조회
#  파라미터 없이 호출하면 항상 빈 결과. ratingGubn: R=등급, O=전망, RO=둘다
# --------------------------------------------------------------------------
def fetch_nice(session, search_type="0"):
    print("  [NICE신용평가] 수집 중...")
    r = session.get(
        NICE_LIST_URL,
        params=[("today", TODAY), ("cmpCd", ""),
                ("strDate", NICE_FROM), ("endDate", TODAY),
                ("ratingGubn", "RO"), ("searchType", search_type)],
        timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    p = TableParser()
    p.feed(r.text)
    if not p.rows:
        raise StructureError("표 행을 하나도 찾지 못했습니다 (페이지 구조 변경 또는 차단)")
    if not any("기업명" in row and ("확정일" in row or "등급확정일" in row) for row in p.rows):
        raise StructureError("NICE: 기업명/등급확정일 헤더 누락")

    # 상세 페이지 키(cmpCd)는 회사명 링크에만 들어 있어 표 텍스트로는 얻을 수 없다.
    # 리포트를 받으려면 이 키가 필요하다.
    cmp_codes = {nm.strip(): cd for cd, nm in NICE_CMP_RE.findall(r.text)}

    results, raw_count, n_cancel = [], 0, 0
    for row in p.rows:
        cells = [c.strip() for c in row]
        if "기업명" in cells or cells[:2] == ["등급", "전망"]:
            continue
        if not cells or not any(is_date(c) for c in cells):
            continue
        raw_count += 1
        if not is_date(cells[-1]):
            raise StructureError("NICE: 날짜 열 위치 변경")

        n = len(cells)
        if search_type in ("6", "7") and n == 7:
            e = dict(company=cells[0], eval_type=cells[3], prev_rating=cells[4],
                     prev_outlook="", new_rating=cells[5], new_outlook="", date=cells[6],
                     instrument="SF" if search_type == "6" else "유동화익스포져")
        elif n == 10:
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
            raise StructureError(f"NICE: 일부 데이터 행 열 수 변경 ({n}열)")

        if e["new_rating"] in NON_RATING_STATUSES:
            n_cancel += 1
            continue
        if not e["company"] or not is_valid_rating(e["new_rating"]):
            raise StructureError(f"NICE: 회사/등급 필드 변경 ({e['company']}: {e['new_rating']!r})")
        e["prev_rating"] = valid_previous(e["prev_rating"], "NICE")
        e.update(source="NICE신용평가", change_code="",
                 cmp_cd=cmp_codes.get(e["company"].strip(), ""))
        results.append(e)

    label = {"0": "NICE신용평가", "6": "NICE SF", "7": "NICE 유동화익스포져"}[search_type]
    validate_html_count(r.text, raw_count, label)
    changed = checked_changes(results, label)
    SOURCE_STATS[label].update(raw=raw_count, excluded_cancel=n_cancel)
    attach_nice_pdfs(session, changed)
    n_pdf = sum(len(c.get("pdfs") or []) for c in changed)
    print(f"  [NICE신용평가] 원본 {len(results)}건 -> 변동 {len(changed)}건"
          f", 리포트 {n_pdf}개 매칭 ({NICE_FROM}~{TODAY})")
    return changed


def fetch_nice_sf(session):
    return fetch_nice(session, "6")


def fetch_nice_exposure(session):
    return fetch_nice(session, "7")


def attach_nice_pdfs(session, changes) -> None:
    """변동 건마다 기업 상세 페이지를 열어 같은 날짜의 의견서를 찾아 붙인다.

    목록 페이지에는 파일 링크가 없어 회사별로 한 번씩 더 요청해야 한다.
    첨부는 부가 기능이므로 여기서 실패해도 브리핑은 그대로 나가야 한다.
    """
    for e in changes:
        e.setdefault("pdfs", [])
        cmp_cd = e.get("cmp_cd")
        if not cmp_cd:
            continue
        try:
            r = session.get(NICE_COMP_URL, params={"cmpCd": cmp_cd},
                            headers={"Referer": NICE_LIST_URL}, timeout=TIMEOUT)
            r.raise_for_status()
            r.encoding = "utf-8"
        except Exception as exc:
            print(f"    상세 조회 실패 {e['company']}: {type(exc).__name__}: {exc}")
            continue

        # 한 회사에 여러 등급내역이 쌓여 있다. 이번 변동 날짜가 들어간 행만 본다.
        for block in re.findall(r"<tr[^>]*>.*?</tr>", r.text, flags=re.S):
            if e.get("date", "") not in block:
                continue
            for doc_id, alt in NICE_PDF_RE.findall(block):
                if NICE_PDF_KIND not in alt:
                    continue
                e["pdfs"].append({"src": "nice", "docId": doc_id,
                                  "file": f"{e['company']}_{e.get('date', '')}.pdf",
                                  "kind": alt.strip()})
                break
            if e["pdfs"]:
                break
        del e["pdfs"][MAX_PDF_PER_ITEM:]


# --------------------------------------------------------------------------
#  Watch 목록 — 등급/전망 목록에 나오지 않는 검토 등록을 별도로 수집한다.
# --------------------------------------------------------------------------
def fetch_nice_watch(session):
    r = session.get(NICE_WATCH_URL, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    p = TableParser()
    p.feed(r.text)
    if not any("등급감시부호" in row and "등록일" in row for row in p.rows):
        raise StructureError("NICE Watch 헤더를 찾지 못했습니다")
    results = []
    dated_rows = 0
    for cells, links in zip(p.rows, p.row_links):
        if "등급감시부호" in cells and "등록일" in cells:
            continue
        if len(cells) == 1 and "등록된 정보가 없습니다." in cells[0]:
            continue
        dated_rows += 1
        if len(cells) != 6 or not cells[0] or not is_date(cells[4]) or not is_valid_rating(cells[2]):
            raise StructureError("NICE Watch 데이터 행 구조가 맞지 않습니다")
        if cells[3] not in WATCH_MARKS or not cells[3]:
            raise StructureError("알 수 없는 NICE Watch 부호: " + cells[3])
        if not WATCH_FROM <= cells[4] <= TODAY.replace("-", "."):
            continue
        cmp_cd = ""
        for link in links:
            hit = re.search(r"goView\(\s*'\w+'\s*,\s*'(\d+)'", link)
            if hit:
                cmp_cd = hit[1]
                break
        results.append(dict(source="NICE신용평가", company=cells[0],
            date=cells[4], prev_rating="", new_rating=cells[2],
            prev_outlook="", new_outlook="", new_watch=watch_name(cells[3]),
            watch_reason=cells[5], instrument=cells[1], cmp_cd=cmp_cd,
            event_type="watch_registration", eval_type="Watch 등록",
            source_url=NICE_WATCH_URL, pdfs=[]))
    # Watch 등록일은 이번 평가일과 다를 수 있어 의견서를 임의 매칭하지 않는다.
    if not dated_rows and "등록된 정보가 없습니다." not in r.text:
        raise StructureError("NICE Watch: 0건을 확인할 명시적 원본 표시가 없습니다")
    changed = checked_changes(results, "NICE Watch")
    SOURCE_STATS["NICE Watch"]["raw"] = dated_rows
    print(f"  [NICE Watch] 최근 30일 등록 {len(changed)}건")
    return changed


def fetch_kr_watch(session):
    r = session.get(KR_WATCH_URL, params={"MENU_ID": "420", "CONTENTS_NO": "1", "SITE_NO": "2"}, timeout=TIMEOUT)
    r.raise_for_status()
    blocks = r.json().get("data")
    if not isinstance(blocks, dict):
        raise StructureError("한국기업평가 Watch data 블록이 없습니다")
    items = json_tables(blocks, "한국기업평가 Watch", ("data1", "data2"))
    hist = load_history()
    results = []
    for row in items:
        date = row.get("RGDT") or ""
        if not is_date(date):
            raise StructureError("한국기업평가 Watch 등록일이 없습니다")
        if not WATCH_FROM <= date <= TODAY.replace("-", "."):
            continue
        company = (row.get("COMP_NM") or "").strip()
        rating = (row.get("GRD_NM") or "").strip()
        status = watch_name(row.get("CRAL_NM") or row.get("CRAL_CD") or "")
        if not company or not is_valid_rating(rating) or not status:
            raise StructureError("한국기업평가 Watch 필수 필드가 없습니다")
        previous = [h for h in hist.values() if h.get("source") == "한국기업평가"
                    and h.get("company") == company and h.get("date") == date
                    and (not h.get("instrument") or h["instrument"] == row.get("BOND_KIND_KRNM"))
                    and clean_rating(h.get("new_rating", "")) == clean_rating(rating)]
        # 종전 등급/전망을 알고 있는 기존 이력은 같은 키로 보완한다.
        entry = dict(previous[0]) if previous else dict(source="한국기업평가",
            company=company, date=date, prev_rating="", new_rating=rating,
            prev_outlook="", new_outlook="", event_type="watch_registration")
        entry.update(new_watch=status, watch_reason=row.get("RSN") or "",
                     eval_type=entry.get("eval_type") or "Watch 등록",
                     source_url="https://www.korearatings.com/cms/frCmnCon/index.do?MENU_ID=420",
                     opn_file=row.get("RTNG_OPN_FILE_NM") or "",
                     svc_id=row.get("SVC_ID") or "", eval_seq=row.get("EVAL_SEQNO"),
                     comp_cd=row.get("COMP_CD") or "", pdfs=[])
        # 기존에 알려진 상품 구분이 없는 종전 이력은 키를 바꾸지 않는다.
        if not previous:
            entry["instrument"] = row.get("BOND_KIND_KRNM") or ""
        entry["kind"] = change_kind(entry) if not previous else "검토변경"
        results.append(entry)
    # 같은 등급의 CP/STB나 여러 회차는 종전 이력의 단위로 중복 제거한다.
    by_key = {item_key(x): x for x in results}
    changed = list(by_key.values())
    for x in changed:
        x["kind"] = "검토변경"
    SOURCE_STATS["한국기업평가 Watch"] = {"raw": len(items), "parsed": len(results), "changes": len(changed)}
    SOURCE_STATS["한국기업평가 Watch"]["latest_date"] = max((r.get("date", "") for r in results), default="")
    attach_kr_pdfs(changed)
    print(f"  [한국기업평가 Watch] 최근 30일 등록/보완 {len(changed)}건")
    return changed


# --------------------------------------------------------------------------
#  수집 — 3회 재시도
# --------------------------------------------------------------------------
def scrape_all():
    session = requests.Session()
    session.headers.update(HEADERS)
    all_data, errors = [], []

    for fetcher in (fetch_kr, fetch_kis, fetch_nice, fetch_nice_sf, fetch_nice_exposure,
                    fetch_kr_watch, fetch_nice_watch):
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
            errors.append(f"{fetcher.__name__}: {type(last_err).__name__}: {last_err}")

    print(f"\n  === 3사 합계: 변동 {len(all_data)}건, 실패 {len(errors)}곳 ===")
    required = {"한국기업평가", "한국신용평가", "NICE신용평가", "NICE SF",
                "NICE 유동화익스포져", "한국기업평가 Watch", "NICE Watch"}
    for label in sorted(required - SOURCE_STATS.keys()):
        errors.append(f"{label}: 수집 검증 결과 미완료")
    # 리포트를 같은 세션으로 받아야 쿠키가 유지된다.
    # 공시와 Watch 목록에 함께 있는 건은 검토 정보가 있는 결과를 합친다.
    combined = {}
    for item in all_data:
        key = item_key(item)
        existing = combined.get(key)
        if existing:
            existing.update({k: v for k, v in item.items() if v not in ("", None, [])})
        else:
            combined[key] = item
    return list(combined.values()), errors, session


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
    if any(c.get("new_watch") for c in changes):
        return format_plain(changes)
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
                       "전망변경/검토변경/신규평정). 이 분류를 그대로 따르고 임의로 바꾸지 말 것.\n\n"
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


ORDER = {"등급하향": 0, "등급변경": 1, "등급상향": 2, "전망변경": 3, "검토변경": 3, "신규평정": 4, "변경": 5}


def format_plain(changes):
    lines = [f"📊 [{TODAY}] 신용등급 변동 내역 ({len(changes)}건)\n"]
    for c in sorted(changes, key=lambda c: (0 if c.get("new_rating") == "D" else 1,
                                            ORDER.get(c.get("kind"), 9))):
        kind = c.get("kind") or change_kind(c)
        mark = "⚠️ " if c.get("new_rating") == "D" or kind == "등급하향" else ""
        lines.append(f"- {mark}[{kind}] {c['company']} | {describe_change(c)} | "
                     f"{c['source']} [{c.get('date', '')}]"
                     + (f" · {c['notification_note']}" if c.get("notification_note") else ""))
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


def download_kr_pdf(session, meta) -> bytes:
    # 서버가 쿼리스트링을 직접 파싱한다. 암호화 파일명에 들어 있는 '='·'@' 를
    # 퍼센트 인코딩하면 값이 달라져 "파일이 존재하지 않습니다" 가 돌아온다.
    # 사이트 JS 도 인코딩 없이 이어붙이므로 그대로 맞춘다.
    url = (KR_DOWN_URL + "?dumm=asdf"
           + "&encFileNm=" + meta["encFileNm"]
           + "&encSvcSeqNo=" + meta["encSvcSeqNo"]
           + "&evalNo=" + meta["evalNo"]
           + "&rptNo=03&fileName=&compCd=" + meta["compCd"] + "&compNm=")
    r = session.get(url, headers={"Referer": KR_LIST_URL}, timeout=(30, 120))
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"PDF 가 아닙니다 ({len(r.content)}바이트)")
    return r.content


def download_nice_pdf(session, meta) -> bytes:
    r = session.get(NICE_DOWN_URL, params={"docId": meta["docId"]},
                    headers={"Referer": NICE_COMP_URL}, timeout=(30, 120))
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"PDF 가 아닙니다 ({len(r.content)}바이트)")
    return r.content


def pdf_link(meta):
    """웹 리포트에서 평가사 원본 PDF 로 바로 가는 링크.

    파일을 저장소에 올려 배포하면 평가사 저작물을 재배포하는 셈이 된다.
    주소만 넘기고 내려받기는 평가사 서버가 하게 둔다.

    세 곳 모두 로그인·쿠키·리퍼러 없이 열리는 것을 확인했다. 다만 한국신용평가만
    POST 라 <a> 로는 못 걸어서, 화면에서 폼으로 보내도록 필드를 함께 넘긴다.
    """
    if not meta:
        return None
    src = meta.get("src")
    if src == "nice":
        return {"method": "get", "url": NICE_DOWN_URL + "?docId=" + meta["docId"]}
    if src == "kr":
        # 서버가 쿼리스트링을 직접 파싱한다. download_kr_pdf 와 같은 이유로
        # 인코딩 없이 그대로 이어붙인다.
        return {"method": "get", "url": (
            KR_DOWN_URL + "?dumm=asdf"
            + "&encFileNm=" + meta["encFileNm"]
            + "&encSvcSeqNo=" + meta["encSvcSeqNo"]
            + "&evalNo=" + meta["evalNo"]
            + "&rptNo=03&fileName=&compCd=" + meta["compCd"] + "&compNm=")}
    return {"method": "post", "url": KIS_DOWN_URL, "fields": {
        "fileName": meta.get("file", ""), "fileTitle": meta.get("title", ""),
        "menuCd": meta.get("menuCd", ""), "gubun": meta.get("gubun", ""),
        "writedate": meta.get("writedate", ""), "freeYn": ""}}


def download_pdf(session, meta) -> bytes:
    src = meta.get("src")
    if src == "nice":
        return download_nice_pdf(session, meta)
    if src == "kr":
        return download_kr_pdf(session, meta)
    return download_kis_pdf(session, meta)


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
    """등급변동 건에 매칭된 평가 리포트를 이어서 보낸다.

    브리핑은 이미 나갔으므로 여기서 실패해도 전체를 실패로 만들지 않는다.
    리포트를 못 받은 것과 브리핑이 안 간 것은 심각도가 다르다.

    3사 모두 로그인 없이 받아진다. 한국기업평가는 평가의견(신용평가요지)까지만
    무료이고, 전체 평가리포트는 유료 회원 전용이라 대상에서 뺐다.
    """
    jobs = [(c, p) for c in changes for p in (c.get("pdfs") or [])][:MAX_PDF_PER_RUN]
    if not jobs:
        print("  첨부할 리포트 없음")
        return 0

    sent = failed = skipped = 0
    seen = set()
    for c, meta in jobs:
        try:
            content = download_pdf(session, meta)
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
def backfill_pdf_links(session, hist: dict) -> None:
    """이력에 링크가 비어 있는 건을 뒤늦게 채운다.

    수집 창(7일, NICE 14일)을 벗어난 뒤 이력에 남은 건은 링크를 못 받는다.
    창 자체를 넓히면 그동안 발송되지 않은 옛 변동이 한꺼번에 텔레그램으로
    쏟아지므로, 링크 채우기만 따로 넓게 조회한다. 여기서 찾은 것은 화면에만
    쓰고 발송 대상에는 넣지 않는다.

    한 건당 한 번만 시도한다(pdf_checked). 의견서가 아예 없는 건도 있어서,
    표시해 두지 않으면 매일 같은 조회를 반복하게 된다.
    """
    todo = [r for r in hist.values() if not r.get("pdf") and not r.get("pdf_checked")
            and r.get("event_type") != "watch_registration"]
    if not todo:
        return
    oldest = min((r.get("date") or "9999.99.99") for r in todo)
    if oldest == "9999.99.99":
        return
    start_dot = oldest                      # 2026.08.25
    start_dash = oldest.replace(".", "-")   # 2026-08-25
    print(f"  링크 없는 {len(todo)}건 보정 시도 ({start_dash} ~ {TODAY})")

    found = 0
    by_source = {}
    for r in todo:
        by_source.setdefault(r.get("source", ""), []).append(r)

    # --- 한국기업평가 ---
    if by_source.get("한국기업평가"):
        try:
            session.get(KR_LIST_URL, timeout=TIMEOUT)
            params = [("MENU_ID", "360"), ("CONTENTS_NO", "1"), ("SITE_NO", "2"),
                      ("COMP_CD", ""), ("STDT", start_dash), ("ENDT", TODAY)] +                      [("SVCTY_CD", c) for c in
                      ("01", "07", "02", "03", "10", "11", "05", "09", "04", "06", "08")]
            resp = session.post(
                "https://www.korearatings.com/ajaxf/frDisclosureSvc/getRatingDisclosureList.do",
                headers={"Referer": KR_LIST_URL,
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                         "X-Requested-With": "XMLHttpRequest"},
                data=params, timeout=TIMEOUT)
            resp.encoding = "utf-8"
            items = []
            for v in (resp.json().get("data") or {}).values():
                if isinstance(v, dict) and "Data" in v:
                    items.extend(v["Data"])
            index = {}
            for it in items:
                if not (it.get("RTNG_OPN_FILE_NM") or "").strip():
                    continue
                index.setdefault(((it.get("COMP_NM") or "").strip(),
                                  it.get("EVAL_DT") or ""), it)
            for r in by_source["한국기업평가"]:
                it = index.get((r.get("company", "").strip(), r.get("date", "")))
                if it:
                    r["pdf"] = pdf_link({
                        "src": "kr", "encFileNm": it["RTNG_OPN_FILE_NM"].strip(),
                        "encSvcSeqNo": (it.get("SVC_ID") or "").strip(),
                        "evalNo": str(it.get("EVAL_SEQNO")),
                        "compCd": (it.get("COMP_CD") or "").strip()})
                    found += 1
        except Exception as exc:
            print(f"    한국기업평가 보정 실패: {type(exc).__name__}: {exc}")

    # --- 한국신용평가 (목록이 기본 5일치라 기간을 지정해 다시 받는다) ---
    if by_source.get("한국신용평가"):
        try:
            resp = session.post(KIS_LIST_URL,
                                data={"tabType": "0", "searchYn": "Y",
                                      "startDt": start_dot, "endDt": TODAY.replace("-", ".")},
                                headers={"Referer": KIS_LIST_URL}, timeout=TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            index = {}
            for menu_cd, gubun, title, fname, kind, wdate in KIS_PDF_RE.findall(resp.text):
                index.setdefault((title.strip(), wdate), {
                    "src": "kis", "menuCd": menu_cd, "gubun": gubun, "title": title.strip(),
                    "file": fname, "writedate": wdate, "kind": kind.strip()})
            for r in by_source["한국신용평가"]:
                meta = index.get((r.get("company", "").strip(),
                                  r.get("date", "").replace(".", "")))
                if meta:
                    r["pdf"] = pdf_link(meta)
                    found += 1
        except Exception as exc:
            print(f"    한국신용평가 보정 실패: {type(exc).__name__}: {exc}")

    # --- NICE (목록에서 cmpCd 를 얻고 기업 상세에서 의견서를 찾는다) ---
    if by_source.get("NICE신용평가"):
        try:
            resp = session.get(NICE_LIST_URL,
                               params=[("today", TODAY), ("cmpCd", ""),
                                       ("strDate", start_dash), ("endDate", TODAY),
                                       ("ratingGubn", "RO"), ("searchType", "0")],
                               timeout=TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            codes = {nm.strip(): cd for cd, nm in NICE_CMP_RE.findall(resp.text)}
            for r in by_source["NICE신용평가"]:
                cmp_cd = codes.get(r.get("company", "").strip())
                if not cmp_cd:
                    continue
                d = session.get(NICE_COMP_URL, params={"cmpCd": cmp_cd},
                                headers={"Referer": NICE_LIST_URL}, timeout=TIMEOUT)
                d.raise_for_status()
                d.encoding = "utf-8"
                for block in re.findall(r"<tr[^>]*>.*?</tr>", d.text, flags=re.S):
                    if r.get("date", "") not in block:
                        continue
                    hit = next((doc for doc, alt in NICE_PDF_RE.findall(block)
                                if NICE_PDF_KIND in alt), None)
                    if hit:
                        r["pdf"] = pdf_link({"src": "nice", "docId": hit})
                        found += 1
                        break
        except Exception as exc:
            print(f"    NICE 보정 실패: {type(exc).__name__}: {exc}")

    for r in todo:
        r["pdf_checked"] = TODAY
    print(f"  링크 {found}건 보정 완료 (나머지 {len(todo) - found}건은 의견서 없음)")


def write_html(hist: dict, errors, path="report.html") -> None:
    """휴대폰에서 볼 수 있는 정적 리포트. 템플릿의 자리표시자에 JSON 을 끼워 넣는다."""
    tpl = Path("report_template.html")
    if not tpl.exists():
        print("  report_template.html 이 없어 HTML 생성을 건너뜁니다.")
        return

    keys = RECORD_FIELDS
    records = []
    for r in hist.values():
        rec = {k: (r.get(k) or "") for k in keys}
        if r.get("pdf"):
            rec["pdf"] = r["pdf"]
        records.append(rec)
    payload = {
        "meta": {
            "generatedAt": NOW.strftime("%Y-%m-%d %H:%M") + " KST",
            "keepDays": HISTORY_KEEP_DAYS,
            "errors": list(errors or []),
        },
        "records": records,
    }
    html = tpl.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    Path(path).write_text(html, encoding="utf-8")
    print(f"  {path} 생성 ({len(records)}건, {len(html):,} bytes)")
    return Path(path)


def run_monitor() -> int:
    SOURCE_STATS.clear()
    health = read_json(HEALTH_PATH, {})
    # 깨진 전송/변동 이력을 비우고 덮어쓰지 않는다.
    state = load_state()
    load_history()
    print(f"📊 신용등급 모니터링 v7 — {NOW:%Y-%m-%d %H:%M} KST")
    print(f"   조회: {WEEK_AGO} ~ {TODAY} (NICE: {NICE_FROM} ~ {TODAY})")
    print("=" * 60)

    print("\n[1/6] 3사 수집...")
    data, errors, session = scrape_all()

    print("\n[2/6] 기존 발송분 제외...")
    fresh = [d for d in data if notification_key(d) not in state]
    for d in fresh:
        if d.get("new_watch") and item_key(d) in state:
            d["notification_note"] = "기존 공시의 검토 상태 보완"
    print(f"  변동 {len(data)}건 중 신규 {len(fresh)}건 (기발송 {len(data) - len(fresh)}건 제외)")

    issues = list(errors) + volume_issues(health, SOURCE_STATS, WATCH_FROM)
    delayed = [r for r in fresh if health.get("last_success")
               and r.get("date", "").replace(".", "-") < health["last_success"]]
    if delayed:
        names = ", ".join(f"{r['company']} [{r['date']}]" for r in delayed[:5])
        issues.append(f"보충조회에서 미발송 공시 발견 {len(delayed)}건 (지연 공시/누락 가능): {names}")
    alerts = pending_alerts(health, issues)

    print("\n[3/6] 브리핑 작성...")
    if fresh:
        briefing = analyze(fresh)
    elif not issues:
        briefing = f"📊 [{TODAY}] 신용등급 변동 브리핑\n\n✅ 신규 등급변동 내역이 없습니다."
    else:
        briefing = ""
    if alerts:
        briefing += ("\n\n" if briefing else "") + f"⚠️ [{TODAY}] 신용등급 누락 감지/수집 검증 경고\n"
        briefing += "\n".join("- " + message for message in alerts)
        briefing += "\n수집 결과가 불완전할 수 있습니다. 실행 기록을 확인하세요."
        if os.environ.get("GITHUB_RUN_ID"):
            briefing += f"\nhttps://github.com/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    elif not issues and health.get("active_issues"):
        briefing += "\n\n✅ 이전 수집 검증 경고가 해소됐습니다."

    print("\n[4/6] 브리핑 전송...")
    if briefing:
        send_tg(briefing)
    else:
        print("  동일 미해결 경고는 이미 알림 — 중복 발송 생략")

    print("\n[5/6] 리포트 첨부...")
    send_reports(session, fresh)

    # 전송에 성공한 건만 이력에 남긴다. 먼저 저장하면 전송 실패 시 영영 누락된다.
    if fresh:
        for d in fresh:
            state[notification_key(d)] = TODAY
        save_state(state)

    # 웹 리포트는 발송 여부와 무관하게 수집된 전부를 쌓는다. 텔레그램은 '새 소식'을
    # 알리는 것이고, 이쪽은 '지금까지 무슨 일이 있었나'를 보는 화면이다.
    print("\n[6/6] 웹 리포트 갱신...")
    hist = load_history()
    for d in data:
        rec = hist.setdefault(item_key(d), {"first_seen": TODAY})
        rec.update({k: d[k] for k in RECORD_FIELDS if k in d})
        link = pdf_link((d.get("pdfs") or [None])[0])
        if link:
            rec["pdf"] = link
    backfill_pdf_links(session, hist)
    save_history(hist)
    report_path = write_html(hist, issues)
    if not report_path:
        raise IntegrityError("웹 리포트가 생성되지 않았습니다")
    verify_persisted(data, HISTORY_PATH, STATE_PATH, report_path,
                     item_key, notification_key, RECORD_FIELDS)
    write_json(HEALTH_PATH, update_health(health, issues, TODAY, dict(SOURCE_STATS)))
    github_output("report_ready", "true")
    print(f"  수집/발송/웹 교차 검증 완료: {len(data)}건, {len(issues)}개 경고")

    if issues:
        mark_failure_handled()
        print(f"\n⚠️ 완료 (일부 수집 실패: {', '.join(errors)})")
        return 1
    print("\n✅ 완료")
    return 0


def mark_failure_handled():
    github_output("failure_handled", "true")


def github_output(name, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def main() -> int:
    try:
        return run_monitor()
    except Exception as exc:
        message = f"실행/저장 검증 실패: {type(exc).__name__}: {exc}"
        try:
            health = read_json(HEALTH_PATH, {})
        except Exception:
            health = {}
        if pending_alerts(health, [message]):
            send_tg(f"⚠️ [{TODAY}] 신용등급 모니터링 누락 방지 경고\n{message}\n정상적인 변동 없음으로 판단하지 마세요.")
        write_json(HEALTH_PATH, update_health(health, [message], TODAY, dict(SOURCE_STATS)))
        mark_failure_handled()
        print(message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
