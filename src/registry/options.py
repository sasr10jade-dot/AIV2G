"""가입 선택지·입력 규칙 — 직업군, 시·도, 번호판 형식, 개인정보 수집·이용 동의 문구.

직업군과 거주지는 개인정보라 자유 입력을 최소화한다: 직업군은 고정 선택지, 거주지는 시·도 선택 +
시·군·구까지만 받는다(읍·면·동·상세주소는 받지 않는다). 동의 문구를 바꾸면 CONSENT_VERSION을
올려, 어떤 문구에 동의했는지 추적할 수 있게 한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.paths import PROJECT_ROOT

VEHICLE_MODELS_CONFIG = PROJECT_ROOT / "config" / "vehicle_models.json"

# 값은 저장용 코드, 라벨은 화면 표시용
OCCUPATIONS = {
    "office": "회사원",
    "self_employed": "자영업",
    "homemaker": "주부",
    "student": "학생",
    "other": "기타",
}

SIDO = (
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시", "울산광역시",
    "세종특별자치시", "경기도", "강원특별자치도", "충청북도", "충청남도", "전북특별자치도",
    "전라남도", "경상북도", "경상남도", "제주특별자치도",
)

# 예: 43오3450, 123가4567 — 숫자 2~3자리 + 한글 1자 + 숫자 4자리
PLATE_RE = re.compile(r"^\d{2,3}[가-힣]\d{4}$")
# 한글이고 시·군·구로 끝나는 것만 허용 — 읍·면·동·리나 번지·상세주소는 받지 않는다. 예: 북구, 수원시 영통구
SIGUNGU_RE = re.compile(r"^[가-힣]{1,8}(시|군|구)([ ][가-힣]{1,8}(시|군|구))?$")

REGIONS_CONFIG = Path(__file__).resolve().parents[2] / "config" / "regions.json"
_regions_cache: dict | None = None


def load_regions() -> dict[str, list[str]]:
    """{시·도: [시·군·구]} — config/regions.json. 가입 화면의 선택지이자 서버 검증 기준이다."""
    global _regions_cache
    if _regions_cache is None:
        _regions_cache = json.loads(REGIONS_CONFIG.read_text(encoding="utf-8"))["regions"]
    return _regions_cache


def sigungu_allowed(sido: str, sigungu: str) -> bool:
    """선택한 시·도에 있는 시·군·구인가. 시·도가 목록에 없으면(설정 누락) 판단하지 않고 통과시킨다."""
    names = load_regions().get(sido)
    return True if names is None else sigungu in names


CONSENT_VERSION = "profile-v1"
POLICY_URL = "/privacy-policy"
POLICY_VERSION = "policy-v0.1-draft"
CONSENT_TEXT = (
    "[개인정보 수집·이용 동의]\n"
    "• 수집 항목: 차종, 차량 번호판, 직업군, 거주지(시·군·구까지)\n"
    "• 이용 목적: 차량 확인 및 승인, 내 상황에 맞는 충전 시간·요금·수요 안내(맞춤 알림)\n"
    "• 보유 기간: 회원 탈퇴 시까지(탈퇴하면 지체 없이 삭제)\n"
    "• 동의를 거부할 수 있으나, 거부하면 가입할 수 없습니다.\n"
    "• 자세한 내용은 개인정보 처리방침에서 확인할 수 있습니다."
)


class VehicleModelCatalog:
    def __init__(self, data: dict):
        self.verified = bool(data.get("verified", False))
        self.models: dict[str, dict] = data.get("models", {})

    def has(self, model_id: str) -> bool:
        return model_id in self.models

    def label(self, model_id: str) -> str:
        return self.models[model_id]["label"]

    def public_list(self) -> list[dict]:
        return [{"id": k, "label": v["label"], "maker": v.get("maker", "")} for k, v in self.models.items()]


def load_catalog(path: Path | None = None) -> VehicleModelCatalog:
    return VehicleModelCatalog(json.loads(Path(path or VEHICLE_MODELS_CONFIG).read_text(encoding="utf-8")))
