#!/bin/sh
# 일 배치(scripts/run_daily_batch.py — 데이터 결합·예측 갱신·아크릴 오차·충전 계획/앱 추천)를 macOS launchd에 매일 04:30 등록한다.
# 주간 재학습(kr.aiv2g.retrain, 월 05:00)보다 먼저 끝나도록 04:30에 둔다. 웹 서비스 포트·다른 서비스는 건드리지 않는다.
# 사용: scripts/register_daily_launchd.sh        (해제: launchctl bootout gui/$(id -u)/kr.aiv2g.daily)
# 결과: data/batch/daily/<날짜>/, data/batch/daily_history.jsonl, output/daily.log, 예외는 웹앱 관리 > 예외 기록
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/kr.aiv2g.daily.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/output"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>kr.aiv2g.daily</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string>
    <string>cd "$ROOT" &amp;&amp; . .venv/bin/activate &amp;&amp; SP=\$(python -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])') &amp;&amp; DYLD_FALLBACK_LIBRARY_PATH="\$SP/sklearn/.dylibs" PYTHONUTF8=1 python scripts/run_daily_batch.py</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>$ROOT/output/daily.log</string>
  <key>StandardErrorPath</key><string>$ROOT/output/daily.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/kr.aiv2g.daily" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "등록 완료: 매일 04:30 — 즉시 실행: launchctl kickstart gui/$(id -u)/kr.aiv2g.daily"
