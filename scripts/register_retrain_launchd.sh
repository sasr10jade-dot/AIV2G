#!/bin/sh
# 운영 재학습 사이클 배치(scripts/run_retrain_cycle.py)를 macOS launchd에 주 1회(월 05:00) 등록한다 — 요구사항 Y2B02.
# 사용: scripts/register_retrain_launchd.sh        (해제: launchctl bootout gui/$(id -u)/kr.aiv2g.retrain)
# 결과: data/batch/retrain_history.jsonl(사이클 결과), output/retrain.log(실행 로그), 예외는 웹앱 관리 > 예외 기록
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/kr.aiv2g.retrain.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/output"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>kr.aiv2g.retrain</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string>
    <string>cd "$ROOT" &amp;&amp; . .venv/bin/activate &amp;&amp; SP=\$(python -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])') &amp;&amp; DYLD_FALLBACK_LIBRARY_PATH="\$SP/sklearn/.dylibs" PYTHONUTF8=1 python scripts/run_retrain_cycle.py</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$ROOT/output/retrain.log</string>
  <key>StandardErrorPath</key><string>$ROOT/output/retrain.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/kr.aiv2g.retrain" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "등록 완료: 매주 월 05:00 — 즉시 실행: launchctl kickstart gui/$(id -u)/kr.aiv2g.retrain"
