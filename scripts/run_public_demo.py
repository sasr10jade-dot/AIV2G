"""외부 공개용 데모 서버 — 임시 저장소·무작위 비밀번호·간편 로그인 없음.

터널(cloudflared 등)로 외부에 열 때 쓴다. 개발용 서버(`/dev`의 간편 로그인)는 클릭 한 번으로 관리자가 될 수 있어 그대로 열면 안 된다.
이 스크립트는 다음을 지킨다.
  - DEMO_* 간편 로그인 환경변수를 지워 간편 로그인을 끈다(.env에 있어도 무시)
  - 계정(모바일 이용자 1, 관리자 1)은 실행할 때마다 새로 만든 무작위 비밀번호로 만들고, 값은 지정한 파일에만 적는다(화면·로그에 출력하지 않음)
  - 계정·동의·알림 저장소는 모두 임시 폴더라 data/를 건드리지 않고, 프로세스를 끝내면 의미가 없어진다
  - 127.0.0.1에만 바인딩한다(외부 노출은 터널이 담당)

사용: python scripts/run_public_demo.py --creds 비밀번호파일.txt [--port 8766]
"""
from __future__ import annotations

import argparse
import os
import secrets
import string
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def strong_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(18))
        if any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw):
            return pw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--creds", type=Path, required=True, help="생성한 계정 정보를 적을 파일(저장소 밖 경로 권장)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-owner-quick-login", action="store_true", help="이용자(모바일) 빠른 로그인 버튼을 켜지 않는다")
    ap.add_argument("--no-admin-quick-login", action="store_true",
                    help="관리자·최고관리자 빠른 로그인을 켜지 않는다. 기본은 켬: 주소를 아는 누구나 관리자 화면(상품권 코드 등록·계정관리 포함)에 "
                         "들어갈 수 있으니 임시 저장소의 데모 데이터만 있는 서버에서, 짧게만 열 것")
    args = ap.parse_args()

    os.environ["DISABLE_CACHE_WARMER"] = "1"
    os.environ["ROOT_REDIRECT"] = "/dev"   # 기본 주소(/)가 개발용 진입 페이지를 열게 한다
    os.environ["SESSION_HTTPS_ONLY"] = "1"   # 터널은 HTTPS — 세션 쿠키에 Secure를 붙인다
    os.environ["TRUST_PROXY_HEADERS"] = "1"   # 터널이 넘기는 실제 접속지를 접속기록·시도 제한에 쓴다
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")   # GEMINI_API_KEY·SESSION_SECRET 만 필요하다
    for key in ("DEMO_ADMIN_USERNAME", "DEMO_ADMIN_PASSWORD", "DEMO_OWNER_USERNAME", "DEMO_OWNER_PASSWORD", "SUPERADMIN_USERNAME", "SUPERADMIN_PASSWORD"):
        os.environ.pop(key, None)   # 간편 로그인·최고관리자 계정 자동 생성을 끈다

    from src.auth import store as auth_store
    from src.pipeline import store
    from src.privacy import consent, pseudonym, vault

    tmp = Path(tempfile.mkdtemp(prefix="v2g_public_"))
    auth_store.USERS_FILE = tmp / "users.json"
    store.CURATED_DB = tmp / "curated.db"
    consent.PRIVACY_DB = tmp / "privacy.db"
    pseudonym.KEY_FILE = tmp / ".pseudo_key"
    vault.KEY_FILE = tmp / ".pii_key"

    owner_pw, admin_pw, super_pw = strong_password(), strong_password(), strong_password()
    demo_code = secrets.token_urlsafe(18)   # 이용자 전용 데모 링크의 비밀 코드
    if args.creds.exists():   # 이미 만든 계정 정보 파일이 있으면 비밀번호를 재사용한다(재시작해도 접속 정보가 바뀌지 않게)
        saved = dict(line.split(": ", 1) for line in args.creds.read_text(encoding="utf-8").splitlines() if ": " in line)
        old_path = saved.get("모바일 데모 링크 경로", "")
        if "code=" in old_path and len(old_path.split("code=", 1)[1].strip()) >= 16:
            demo_code = old_path.split("code=", 1)[1].strip()   # 데모 링크도 재시작해도 그대로 유지한다
        for label, name in (("모바일(이용자)", "owner"), ("PC·어드민(관리자)", "admin"), ("최고관리자", "super")):
            parts = saved.get(label, "").split(" / ")
            if len(parts) == 2 and len(parts[1].strip()) >= 12:
                if name == "owner":
                    owner_pw = parts[1].strip()
                elif name == "admin":
                    admin_pw = parts[1].strip()
                else:
                    super_pw = parts[1].strip()
    auth_store.create_user("demo-owner", owner_pw, "owner", owned_ev_id="EV-003")   # 방전 가능 차종
    auth_store.create_user("demo-admin", admin_pw, "admin")
    auth_store.create_user("demo-super", super_pw, "super_admin")   # 임시 저장소의 데모 최고관리자(.env의 실제 최고관리자와 무관)
    args.creds.parent.mkdir(parents=True, exist_ok=True)
    args.creds.write_text(f"모바일(이용자): demo-owner / {owner_pw}\nPC·어드민(관리자): demo-admin / {admin_pw}\n최고관리자: demo-super / {super_pw}\n모바일 데모 링크 경로: /auth/demo-access?code={demo_code}\n", encoding="utf-8")

    from src.appadmin import service as appadmin
    from src.notify import service as notify

    consent.grant("EV-003", "notify", notify.NOTIFY_CONSENT_VERSION, recorded_by="public-demo-seed")
    notify.create("EV-003", "price_window", "오늘 밤 충전이 가장 저렴해요(예측)",
                  "00시~06시 구간 요금이 가장 낮아요. 출발 전까지 충전을 이 시간에 맞춰보세요.", dedupe_key="pd-1",
                  payload={"en": {"title": "Tonight is the cheapest time to charge (forecast)",
                                  "body": "The 00-06h window has the lowest rate. Plan your charging for that time before you leave."}})
    notify.create("EV-003", "forecast", "내일 저녁 전력 수요가 높을 예정이에요(예측)",
                  "18시~21시 수요가 높아질 것으로 예측돼요. 이 시간대 충전은 피하는 게 좋아요.", dedupe_key="pd-2",
                  payload={"en": {"title": "Power demand is expected to be high tomorrow evening (forecast)",
                                  "body": "Demand is forecast to rise between 18h and 21h. It is best to avoid charging then."}})
    appadmin.notice_create("시범 운영 안내", "AI-V2G 내 차 시범 운영 중이에요. 궁금한 점은 EV Bot에게 물어보세요.", True, True, "public-demo-seed",
                           "Pilot notice", "AI-V2G My Car is in a pilot run. Ask EV Bot if you have questions.")
    print(f"public demo store: {tmp} (계정 정보는 {args.creds} 에 저장)", flush=True)

    import uvicorn
    from src.api import app
    from src.advisory import owner_guide
    # src 모듈이 가져오면서 .env를 다시 읽어 환경변수를 되살릴 수 있어, 서버를 띄우기 직전에 한 번 더 지운다
    # (간편 로그인 가능 여부와 최고관리자 계정은 요청 때마다 환경변수를 읽는다)
    for key in ("DEMO_ADMIN_USERNAME", "DEMO_ADMIN_PASSWORD", "DEMO_OWNER_USERNAME", "DEMO_OWNER_PASSWORD", "SUPERADMIN_USERNAME", "SUPERADMIN_PASSWORD"):
        os.environ.pop(key, None)
    os.environ["DEMO_ACCESS_TOKEN"] = demo_code      # 이용자 전용 데모 링크 켜기(이용자 역할 계정만 열린다)
    os.environ["DEMO_ACCESS_OWNER"] = "demo-owner"
    if not args.no_admin_quick_login:
        os.environ["DEMO_ADMIN_USERNAME"] = "demo-admin"
        os.environ["DEMO_ADMIN_PASSWORD"] = admin_pw
        os.environ["SUPERADMIN_USERNAME"] = "demo-super"   # 빠른 로그인의 최고관리자 계정 — 위에서 만든 임시 데모 계정
        os.environ["SUPERADMIN_PASSWORD"] = super_pw
    if not args.no_owner_quick_login:
        # 모바일 로그인 화면의 '개발용 간편 로그인' 버튼 — 이용자(demo-owner)만. 관리자 빠른 로그인은 켜지 않는다.
        os.environ["DEMO_OWNER_USERNAME"] = "demo-owner"
        os.environ["DEMO_OWNER_PASSWORD"] = owner_pw
    owner_guide.warm_caches_async()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
