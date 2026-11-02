"""회원 탈퇴(개인정보 삭제) — 정보주체가 탈퇴하면 개인정보를 지체 없이 지운다.

지우는 것
- 계정(users.json), 차량 등록 신청 전체(암호화된 번호판·직업군·거주지·운행 입력 포함)
- 가입 앱에서 만든 차량(`EV-U…`)의 추천 기록(그 차량은 이 회원만의 것이다)

남기는 것
- 접속기록·회원 이력(`audit_log`): 접속기록 법정 보관 목적. 보관 기간이 지나면 파기 스크립트(P-d)로 정리한다.
- 동의 이력(`consents`)은 증빙을 위해 남기되, 유효한 동의는 모두 철회 처리한다.
- 기존 OBD 실증 차량(`EV-TORRES-…`)의 차량 데이터·추천 기록: 사람과의 연결만 끊고 데이터는 유지한다.

여러 저장소에 걸친 작업이라 한 번에 되돌릴 수 없다. 그래서 되돌리기 어려운 계정 삭제를 맨 마지막에 하고,
중간에 실패하면 계정이 남아 있어 같은 요청을 다시 보낼 수 있게(멱등) 만든다.
"""
from __future__ import annotations

from src.auth import store as auth_store
from src.pipeline import store
from src.privacy import consent, member_history
from src.registry.service import AUTO_EV_PREFIX, RegistryError


def withdraw_member(username: str, ip: str | None = None) -> dict:
    user = auth_store.get_user(username)
    if not user:
        raise RegistryError("존재하지 않는 계정입니다")
    if user["role"] != "owner":
        raise RegistryError("차량 등록자 계정만 탈퇴할 수 있습니다")

    summary = {"registrations": 0, "advisories": 0, "consents_withdrawn": 0}
    con = store.get_connection()
    try:
        rows = con.execute("SELECT registration_id, ev_id FROM vehicle_registrations WHERE username=?",
                           (username,)).fetchall()
        ev_ids = {r[1] for r in rows if r[1]}
        if user.get("owned_ev_id"):
            ev_ids.add(user["owned_ev_id"])
        for ev_id in ev_ids:
            if ev_id.startswith(AUTO_EV_PREFIX):   # 앱 가입 차량: 이 회원 전용 기록이라 함께 삭제
                summary["advisories"] += con.execute("DELETE FROM advisories WHERE ev_id=?", (ev_id,)).rowcount
        summary["registrations"] = con.execute("DELETE FROM vehicle_registrations WHERE username=?",
                                               (username,)).rowcount
        con.commit()
    finally:
        con.close()

    for ev_id in ev_ids:
        for purpose in consent.PURPOSES:
            if consent.withdraw(ev_id, purpose):
                summary["consents_withdrawn"] += 1

    auth_store.delete_user(username)   # 되돌릴 수 없는 단계는 마지막에
    member_history.record(username, username, "withdraw", ip=ip, note="탈퇴 — 개인정보 삭제, 이력은 접속기록 보관 기간 동안 보존")
    return summary
