#!/bin/sh
# 사내 와이파이 HTTPS용 내부 인증서 만들기 (macOS·Linux, openssl만 사용 — 외부 프로그램 설치 없음)
# 사용: scripts/make_lan_cert.sh [서버IP]   (기본 192.168.35.92)
#
# 왜: 휴대폰 브라우저는 http 주소에서 앱 설치(PWA)·서비스 워커·오프라인을 막는다. 내부망 전용으로
#     HTTPS를 쓰려고 '내부 루트 인증서(CA)'를 만들고, 그 CA로 서버 IP용 인증서를 발급한다.
# 결과(certs/, 저장소에 올리지 않음):
#   certs/lan-ca.key   루트 인증서 개인키 — 서버 밖으로 내보내지 않는다(권한 600)
#   certs/lan-ca.crt   루트 인증서(공개) — 휴대폰에 설치·신뢰한다. /pwa/lan-ca.crt 로 내려받을 수 있다
#   certs/server.key   서버 개인키(권한 600)
#   certs/server.crt   서버 인증서 — IP·localhost용, 유효기간 825일(아이폰 상한)
# 이미 CA가 있으면 CA는 그대로 두고 서버 인증서만 다시 만든다(휴대폰에 다시 설치할 필요 없음).
set -e
IP="${1:-192.168.35.92}"
cd "$(dirname "$0")/.."
mkdir -p certs
chmod 700 certs
cd certs

if [ ! -f lan-ca.key ] || [ ! -f lan-ca.crt ]; then
  openssl genrsa -out lan-ca.key 2048 2>/dev/null
  chmod 600 lan-ca.key
  cat > ca.cnf <<EOF
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = AIV2G LAN Dev CA
O = AIV2G Dev (internal only)
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF
  openssl req -x509 -new -key lan-ca.key -sha256 -days 3650 -config ca.cnf -out lan-ca.crt
  rm -f ca.cnf
  echo "새 루트 인증서를 만들었습니다: certs/lan-ca.crt (휴대폰에 설치·신뢰 필요)"
fi

openssl genrsa -out server.key 2048 2>/dev/null
chmod 600 server.key
cat > server.cnf <<EOF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = ${IP}
O = AIV2G Dev (internal only)
[v3]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:${IP}, IP:127.0.0.1, DNS:localhost
EOF
openssl req -new -key server.key -config server.cnf -out server.csr
openssl x509 -req -in server.csr -CA lan-ca.crt -CAkey lan-ca.key -CAcreateserial \
  -days 825 -sha256 -extfile server.cnf -extensions v3 -out server.crt 2>/dev/null
rm -f server.csr server.cnf
openssl verify -CAfile lan-ca.crt server.crt
echo "서버 인증서: certs/server.crt (${IP}, 825일)"
