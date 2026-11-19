# 사내 PC NICE 수집 (2026-11-19~)

2026-11-19부터 NICE신용평가 웹방화벽이 해외 IP를 HTTP 400으로 막는다.
GitHub Actions(미국)와 Vultr 서버(도쿄) 모두 막히고, 국내 회사망에서는 열린다.

그래서 사내 PC 예약 작업이 평일 08:15에 NICE 공시 표 4종(등급, SF, 유동화익스포져,
Watch)을 읽어 `daily.yml` 실행을 요청한다. 수집 검증, 텔레그램 발송, 웹앱 배포는
기존대로 Actions가 한다. PC가 넘기는 것은 NICE 공개 공시뿐이다.

Vultr 08:27 호출은 예비로 남는다. 오늘 이미 경고 없이 끝난 실행이 있으면 건너뛰고,
PC가 꺼져 있던 날에는 기존처럼 실행된다(한기평·한신평 정상, NICE는 실패 알림).

## 설치

1. 저장소를 PC의 상설 위치에 받는다 (예: `C:\Users\<사용자>\credit-monitoring`).
2. GitHub에서 Fine-grained token을 만든다.
   - Repository access: Only select repositories → `credit-monitoring`
   - Permissions → Repository → Actions: **Read and write** (다른 권한 없음)
3. 토큰을 Windows 자격 증명 관리자에 저장한다. 프롬프트에 토큰을 붙여넣는다(화면에 안 보임).
   ```
   cmdkey /generic:credit-monitoring-github /user:github /pass
   ```
4. 시험 실행 (한 번 실행되고 팀방에 메시지가 간다):
   ```
   python ops\pc\nice_dispatch.py --force
   ```
5. 예약 작업 등록:
   ```
   powershell -ExecutionPolicy Bypass -File ops\pc\register_task.ps1
   ```

## 확인과 해제

- 로그: `%LOCALAPPDATA%\credit-monitoring\pc-trigger.log` (토큰은 기록하지 않음)
- 작업 스케줄러: `CreditMonitor-NICE`
- 수동으로 다시 돌리기: GitHub Actions에서 Run workflow 시 `force_run` 체크
- NICE 차단이 풀리면 해제한다.
  ```
  Unregister-ScheduledTask -TaskName CreditMonitor-NICE -Confirm:$false
  cmdkey /delete:credit-monitoring-github
  ```
- 토큰 만료 전에 새 토큰으로 3번을 다시 실행한다.
