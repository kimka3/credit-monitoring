# Vultr 정시 실행

GitHub의 `schedule` 이벤트 지연을 피하기 위해 Vultr의 systemd timer가 한국시간
월~금 오전 8시 27분에 `daily.yml`의 `workflow_dispatch`를 호출한다. 실제
모니터링, 텔레그램 전송, 상태 커밋과 Pages 배포는 계속 GitHub Actions에서 한다.

Fine-grained personal access token은 `credit-monitoring` 저장소를 선택하고
`Actions: Read and write` 권한만 부여한다. 토큰은 `/etc/credit-monitoring.env`에
권한 `0600`으로 저장한다.

```bash
git clone https://github.com/kimka3/credit-monitoring.git
cd credit-monitoring
sudo bash ops/vultr/install.sh
```

```bash
systemctl list-timers credit-monitor-trigger.timer
journalctl -u credit-monitor-trigger.service --since today
```

`Persistent=false`라 서버가 꺼져 있던 동안의 실행을 뒤늦게 보충하지 않는다.
스크립트도 한국시간 평일과 08:00~09:29를 확인하므로 비정상적으로 늦은 호출은
건너뛴다. `--force`는 설치 검증 같은 명시적 수동 실행에만 사용한다.

2026-09-29 실제 Vultr 호출에서 모니터링·이력 처리·Pages 배포까지 성공한 뒤
GitHub의 `schedule` 항목을 제거했으며 `workflow_dispatch`만 유지한다.
