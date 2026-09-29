#!/usr/bin/env bash
set -Eeuo pipefail

if (( EUID != 0 )); then
  printf 'Run this installer with sudo.\n' >&2
  exit 1
fi

for command in curl systemctl install; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Required command is missing: %s\n' "$command" >&2
    exit 1
  fi
done

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
token="${GITHUB_TOKEN:-}"
if [[ -z "$token" ]]; then
  if [[ ! -t 0 ]]; then
    printf 'Set GITHUB_TOKEN or run interactively to enter it.\n' >&2
    exit 1
  fi
  read -r -s -p 'GitHub fine-grained token: ' token
  printf '\n'
fi
if [[ ! "$token" =~ ^[A-Za-z0-9_]+$ ]]; then
  printf 'The GitHub token contains unexpected characters.\n' >&2
  exit 1
fi

temp_env="$(mktemp)"
trap 'rm -f -- "$temp_env"' EXIT
printf 'GITHUB_TOKEN=%s\n' "$token" >"$temp_env"

install -d -m 0755 /usr/local/libexec/credit-monitoring
install -m 0755 "$source_dir/trigger-credit-monitor.sh" \
  /usr/local/libexec/credit-monitoring/trigger-credit-monitor.sh
install -m 0644 "$source_dir/credit-monitor-trigger.service" \
  /etc/systemd/system/credit-monitor-trigger.service
install -m 0644 "$source_dir/credit-monitor-trigger.timer" \
  /etc/systemd/system/credit-monitor-trigger.timer
install -m 0600 "$temp_env" /etc/credit-monitoring.env

GITHUB_TOKEN="$token" \
  /usr/local/libexec/credit-monitoring/trigger-credit-monitor.sh --check

systemctl daemon-reload
systemctl enable --now credit-monitor-trigger.timer
if ! systemctl is-active --quiet credit-monitor-trigger.timer; then
  printf 'The timer could not be started.\n' >&2
  systemctl --no-pager status credit-monitor-trigger.timer || true
  exit 1
fi

printf '\nInstalled successfully.\n'
systemctl --no-pager status credit-monitor-trigger.timer || true
printf '\nNext run:\n'
systemctl list-timers credit-monitor-trigger.timer --no-pager
printf '\nThe timer only dispatches on Monday-Friday between 08:00 and 09:29 KST.\n'
