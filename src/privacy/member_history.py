"""회원정보 변경 이력 — 누가·언제·무엇을 어떻게 바꿨는지 회원 단위로 추적한다.

별도 테이블을 만들지 않고 해시체인 감사로그(`audit`)에 `member.<action>` 으로 남긴다 — 위·변조 탐지와
보관 정책을 그대로 따르고, 기록 경로가 하나라 빠뜨릴 일이 줄어든다. target은 항상 회원 아이디다.

기록 원칙
- 기록할 수 있는 필드는 ALLOWED_FIELDS(흰 목록)뿐이다. 그 밖의 필드는 ValueError — 실수로 민감값이 로그에 들어가는 것을 막는다.
- 비밀번호는 값을 남기지 않고 "(변경됨)"만, 번호판은 마스킹 값만 남긴다(호출 측이 원본을 넘겨도 여기서 가린다).
- 본인에게 보이는 이력에서 처리자는 "본인"/"관리자"로만 표시하고 관리자 계정명·접속 IP는 숨긴다. IP는 관리자용 조회에만 포함한다.
"""
from __future__ import annotations

import json
from typing import Iterable

from src.privacy import audit
from src.privacy.pseudonym import mask_plate

PREFIX = "member."

ACTION_LABELS = {
    "signup": "회원가입",
    "registration_submit": "차량 등록 신청",
    "registration_approve": "차량 등록 승인",
    "registration_reject": "차량 등록 반려",
    "profile_update": "회원정보 수정",
    "consent_grant": "동의",
    "consent_withdraw": "동의 철회",
    "admin_view": "관리자 열람",
    "withdraw": "회원 탈퇴",
}

FIELD_LABELS = {
    "vehicle_model": "차종", "occupation": "직업군", "region_sido": "거주 시·도", "region_sigungu": "거주 시·군·구",
    "plate": "번호판", "password": "비밀번호", "status": "상태", "ev_id": "연결 차량",
    "notify_consent": "알림 수신 동의", "v2g_discharge_consent": "방전(V2G) 참여", "incentive_consent": "시범 포인트 적립 동의", "profile_consent": "개인정보 수집·이용 동의",
}
ALLOWED_FIELDS = frozenset(FIELD_LABELS)

def _mask(field: str, value) -> str | None:
    """이력에 저장할 값 — 번호판은 마스킹, 비밀번호는 값 없이 표시만."""
    if value is None:
        return None
    if field == "plate":
        return mask_plate(str(value))
    if field == "password":
        return "(변경됨)"
    return str(value)


def record(actor: str, username: str, action: str, changes: Iterable[tuple] = (), ip: str | None = None,
           note: str | None = None) -> None:
    """changes: (field, before, after) 목록. 값은 마스킹해서 저장한다."""
    if action not in ACTION_LABELS:
        raise ValueError(f"알 수 없는 회원 이력 종류: {action}")
    rows = []
    for field, before, after in changes:
        if field not in ALLOWED_FIELDS:
            raise ValueError(f"이력에 남길 수 없는 필드: {field}")
        rows.append({"field": field, "before": _mask(field, before), "after": _mask(field, after)})
    detail = {"changes": rows, "ip": ip}
    if note:
        detail["note"] = note
    audit.log(actor, PREFIX + action, target=username, detail=detail)


def timeline(username: str, limit: int = 100, for_admin: bool = False) -> list[dict]:
    """회원 한 명의 이력(최신순). 본인용은 처리자를 '본인/관리자'로만, IP는 빼고 돌려준다."""
    out = []
    for e in audit.entries(PREFIX, target=username, limit=limit):
        action = e["action"][len(PREFIX):]
        detail = json.loads(e["detail"]) if e["detail"] else {}
        item = {
            "ts": e["ts"],
            "action": action,
            "label": ACTION_LABELS.get(action, action),
            "by": "본인" if e["actor"] == username else "관리자",
            "changes": [
                {"field": c["field"], "label": FIELD_LABELS.get(c["field"], c["field"]),
                 "before": c["before"], "after": c["after"]}
                for c in detail.get("changes", [])
            ],
            "note": detail.get("note"),
        }
        if for_admin:
            item["actor"] = e["actor"]
            item["ip"] = detail.get("ip")
        out.append(item)
    return out
