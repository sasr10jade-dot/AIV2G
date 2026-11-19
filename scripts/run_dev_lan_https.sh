#!/bin/sh
# 사내 와이파이 개발 서버 — HTTPS (휴대폰 앱 설치·PWA용), 기본 포트 5443
# 사용: scripts/run_dev_lan_https.sh [포트]
#   - 먼저 scripts/make_lan_cert.sh 로 내부 인증서를 만든다(certs/server.crt, certs/server.key)
#   - 휴대폰은 /install 에서 루트 인증서(lan-ca.crt)를 한 번 설치·신뢰한 뒤 https://192.168.35.92:5443/install 로 접속
#   - http 5050 서버와 같은 앱·같은 DB를 쓴다. 로그인 쿠키를 함께 쓰려면 .env의 SESSION_SECRET이 고정돼 있어야 한다
#   - 내부망 전용: 이 포트를 공유기 포트포워딩·터널로 외부에 열지 않는다. 8000·8080·8443 등 다른 서비스 포트와 겹치지 않게 한다
set -e
PORT="${1:-5443}"
cd "$(dirname "$0")/.."
if [ ! -f certs/server.crt ] || [ ! -f certs/server.key ]; then
  echo "certs/server.crt 가 없습니다. 먼저 scripts/make_lan_cert.sh 를 실행하세요." >&2
  exit 1
fi
[ -d .venv ] && . .venv/bin/activate
export PYTHONUTF8=1
export QUICK_LOGIN_LAN_ONLY=1
export ROOT_REDIRECT="${ROOT_REDIRECT:-/dev}"
export LAN_HTTPS_PORT="$PORT"
SP=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
if [ "$(uname)" = "Darwin" ] && [ -f "$SP/sklearn/.dylibs/libomp.dylib" ]; then
  export DYLD_FALLBACK_LIBRARY_PATH="$SP/sklearn/.dylibs${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
fi
echo "AIV2G dev server (HTTPS): https://192.168.35.92:${PORT}/install"
exec python -m uvicorn src.api:app --host 0.0.0.0 --port "$PORT" \
  --ssl-certfile certs/server.crt --ssl-keyfile certs/server.key
