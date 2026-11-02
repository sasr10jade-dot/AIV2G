"""시간대 처리 공용 함수 — 프로젝트의 시각 관례를 한 곳에 둔다.

관례: 분석·최적화·화면의 시각(시나리오 timestamp, 요금 시간대, 피크 시간)은 **한국시간(KST) 기준 tz-naive**다.
외부에서 들어오는 시각은 시간대가 붙은 값(브라우저 `toISOString()`의 UTC `Z`, OCPP 이벤트의 `+00:00`)일 수 있다.
이런 값에서 `tz_localize(None)`을 호출하면 시간대를 변환하지 않고 지워 UTC 시각을 한국시간으로 읽게 되어 9시간 어긋난다 —
반드시 먼저 한국시간으로 변환(`tz_convert`)한 뒤 시간대 정보를 버려야 한다. 그 일을 이 함수 하나가 맡는다.
"""
from __future__ import annotations

import pandas as pd

KST = "Asia/Seoul"


def kst_now() -> pd.Timestamp:
    """지금 시각(한국시간, tz-naive) — 서버 시계 설정(UTC 등)과 무관하게 같은 값을 준다.
    `pd.Timestamp.now()`는 서버의 로컬 시간대를 따라 UTC 서버에서는 9시간 어긋난다."""
    return pd.Timestamp.now(tz=KST).tz_localize(None)


def to_kst_naive(value) -> pd.Timestamp:
    """tz가 있으면 한국시간으로 변환한 뒤 tz를 떼고, tz가 없으면 이미 한국시간으로 보고 그대로 돌려준다."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(KST).tz_localize(None)
    return ts
