"""사내 PC(국내 IP)에서 NICE 공시 4종을 읽어 GitHub Actions 실행을 요청한다.

2026-11-19부터 NICE 웹방화벽이 해외 IP(GitHub Actions, Vultr 도쿄)를 HTTP 400으로 막는다.
이 PC는 NICE 공개 공시 표만 읽어 실행 입력으로 넘기고, 수집 검증·발송·배포는
기존대로 Actions가 한다. 신탁재산 정보는 다루지 않는다.

- 토큰: Windows 자격 증명 관리자의 일반 자격 증명 'credit-monitoring-github'
  (Fine-grained, 이 저장소만, Actions: Read and write). 화면·로그에 쓰지 않는다.
- 같은 날 이미 정상 완료된 실행이 있으면 건너뛴다(--force로 무시).
- 실패하면 실행을 요청하지 않는다. 그러면 Vultr 08:27 실행이 기존처럼 돌고
  NICE 실패를 알린다.
"""
import ctypes
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.vultr.nice_feed import KST, collect  # noqa: E402

REPOSITORY = "kimka3/credit-monitoring"
API = f"https://api.github.com/repos/{REPOSITORY}"
HEALTH_URL = f"https://raw.githubusercontent.com/{REPOSITORY}/main/state/health.json"
CREDENTIAL = "credit-monitoring-github"
LOG = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "credit-monitoring" / "pc-trigger.log"


def log(message):
    line = f"{datetime.now(KST):%Y-%m-%d %H:%M:%S} {message}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass


def read_token(target=CREDENTIAL):
    """자격 증명 관리자에서 토큰을 읽는다 (cmdkey /generic:<target> 로 저장한 값)."""
    from ctypes import wintypes

    class CREDENTIAL_W(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.POINTER(CREDENTIAL_W)()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(pointer)):  # 1 = CRED_TYPE_GENERIC
        raise OSError(f"자격 증명 '{target}'이 없습니다 (오류 {ctypes.get_last_error()})")
    try:
        cred = pointer.contents
        token = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-16-le")
    finally:
        advapi32.CredFree(pointer)
    token = token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", token):
        raise OSError(f"자격 증명 '{target}'의 토큰 형식이 올바르지 않습니다")
    return token


def completed_today(session, today):
    r = session.get(HEALTH_URL, timeout=30)
    r.raise_for_status()
    health = json.loads(r.text)
    return health.get("last_success") == today and not health.get("active_issues")


def dispatch(session, token, bundle):
    r = session.post(f"{API}/actions/workflows/daily.yml/dispatches",
                     json={"ref": "main", "inputs": {"nice_disclosures": bundle}},
                     headers={"Accept": "application/vnd.github+json",
                              "Authorization": f"Bearer {token}",
                              "X-GitHub-Api-Version": "2022-11-28",
                              "User-Agent": "credit-monitoring-pc-trigger"},
                     timeout=30)
    if r.status_code != 204:
        raise RuntimeError(f"GitHub 실행 요청 실패 (HTTP {r.status_code})")


def main(argv=None, now=None):
    argv = sys.argv[1:] if argv is None else argv
    force = "--force" in argv
    now = now or datetime.now(KST)
    today = now.date().isoformat()
    if now.isoweekday() > 5 and not force:
        log("주말이라 건너뜀")
        return 0
    try:
        token = read_token()
    except OSError as exc:
        log(f"중단: {exc}")
        return 1
    try:
        with requests.Session() as session:
            if not force:
                try:
                    if completed_today(session, today):
                        log("오늘 이미 정상 완료된 실행이 있어 건너뜀")
                        return 0
                except (requests.RequestException, ValueError) as exc:
                    log(f"오늘 실행 여부 확인 실패, 계속 진행: {type(exc).__name__}: {exc}")
            bundle = collect()
            log(f"NICE 원본 4종 수집 ({len(bundle):,}자)")
            dispatch(session, token, bundle)
    except Exception as exc:  # 실패하면 Vultr 08:27 실행이 기존처럼 돌며 알린다.
        log(f"실패: {type(exc).__name__}: {exc}")
        return 1
    log("GitHub 실행 요청 완료 (NICE 원본 포함)")
    return 0


if __name__ == "__main__":
    try:
        import truststore  # 회사망의 TLS 검사 인증서를 Windows 인증서 저장소로 신뢰
        truststore.inject_into_ssl()
    except ImportError:
        pass
    sys.exit(main())
