"""충전사업자(CPO) 연계 어댑터 인터페이스 — 사업자마다 다른 API/파일 형식을 하나의
표준 세션 스키마(CHARGER_SESSION_SCHEMA)로 모은다.

사업자가 협의 중이고 연계 방식(API/SFTP/CSV)이 확정되지 않았으므로, 어댑터는 "무엇을
가져오는가"만 약속하고 "어떻게"는 구현체가 정한다. 새 사업자는 `CpoAdapter` 하나만
구현해 `registry`에 등록하면 되고, 동기화·중복제거·품질검증 코드는 바뀌지 않는다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd

# 어댑터가 반환해야 하는 표준 세션 컬럼 (session_id는 사업자 내부 ID — 전역 유일성은 sync에서 보장)
SESSION_COLUMNS = ["session_id", "charger_id", "zone", "start_ts", "duration_h", "energy_kwh", "ev_id"]


class CpoAdapter(ABC):
    #: 사업자 식별 코드(확정 전에는 CPO-A 같은 코드, 확정 후 실명 코드로 교체)
    operator_id: str = ""
    #: 수집 경로 표시값 — charger_sessions.source에 기록된다
    source_kind: str = "cpo"

    @abstractmethod
    def fetch_sessions(self, since: datetime | None = None) -> pd.DataFrame:
        """`since` 이후 시작/갱신된 충전 세션을 SESSION_COLUMNS 형식으로 반환한다."""

    def fetch_charger_status(self) -> pd.DataFrame:
        """충전기 현황(charger_id, status, updated_at). 지원하지 않는 사업자는 빈 표."""
        return pd.DataFrame(columns=["charger_id", "status", "updated_at"])


class CpoDataError(ValueError):
    """사업자 데이터가 표준 형식으로 변환될 수 없을 때."""


def normalize_sessions(df: pd.DataFrame) -> pd.DataFrame:
    """표준 컬럼을 보장하고 타입을 맞춘다. 필수 값이 비었거나 음수인 행은 호출 측에서
    걸러낼 수 있도록 그대로 두지 않고 CpoDataError로 알린다(조용히 버리지 않는다)."""
    missing = [c for c in SESSION_COLUMNS if c not in df.columns and c != "ev_id"]
    if missing:
        raise CpoDataError(f"필수 컬럼 누락: {missing}")
    out = df.copy()
    if "ev_id" not in out.columns:
        out["ev_id"] = None
    out["start_ts"] = pd.to_datetime(out["start_ts"], errors="coerce")
    for col in ("duration_h", "energy_kwh"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    bad = out[["session_id", "charger_id", "start_ts", "duration_h", "energy_kwh"]].isna().any(axis=1)
    bad |= (out["duration_h"] < 0) | (out["energy_kwh"] < 0)
    if bad.any():
        raise CpoDataError(f"형식 오류 행 {int(bad.sum())}건 (인덱스 {list(out.index[bad][:5])} …)")
    out["zone"] = pd.to_numeric(out["zone"], errors="coerce").fillna(0).astype(int)
    return out[SESSION_COLUMNS]
