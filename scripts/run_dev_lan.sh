#!/bin/sh
# 사내 와이파이 개발 서버 (macOS·Linux) — 서버 PC 192.168.35.92, 기본 포트 5050
# 사용: scripts/run_dev_lan.sh [포트]
#   - 0.0.0.0에 바인딩해 같은 와이파이의 PC·휴대폰에서 http://192.168.35.92:5050/ 으로 접속한다
#   - /dev 간편 로그인은 내부망(사설·루프백 IP, 프록시 헤더 없음)에서만 동작한다(QUICK_LOGIN_LAN_ONLY=1)
#   - 이 포트를 공유기 포트포워딩·터널로 외부에 열지 않는다
#   - 같은 PC의 다른 서비스(8000·8080 등)와 겹치지 않는 포트를 쓴다. macOS는 5000번을 AirPlay 수신이 쓴다.
set -e
PORT="${1:-5050}"
cd "$(dirname "$0")/.."
[ -d .venv ] && . .venv/bin/activate
export PYTHONUTF8=1
export QUICK_LOGIN_LAN_ONLY=1
export ROOT_REDIRECT="${ROOT_REDIRECT:-/dev}"
# macOS: LightGBM이 찾는 OpenMP(libomp)가 없으면 scikit-learn에 포함된 것을 쓴다(Homebrew 설치 불필요)
SP=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
if [ "$(uname)" = "Darwin" ] && [ -f "$SP/sklearn/.dylibs/libomp.dylib" ]; then
  export DYLD_FALLBACK_LIBRARY_PATH="$SP/sklearn/.dylibs${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
fi
LAN_IP="$( (ipconfig getifaddr en0 || ipconfig getifaddr en1 || hostname -I | cut -d" " -f1) 2>/dev/null | head -1)"
LAN_IP="${LAN_IP:-192.168.35.92}"
echo "AIV2G dev server: http://${LAN_IP}:${PORT}/  (local: http://127.0.0.1:${PORT}/dev)"
exec python -m uvicorn src.api:app --host 0.0.0.0 --port "$PORT"
