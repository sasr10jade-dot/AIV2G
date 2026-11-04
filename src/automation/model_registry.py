"""모델 레지스트리 — 버전별 모델 아티팩트와 메타데이터를 로컬 디스크에 저장하고,
현재 서빙 중인 활성 버전을 추적한다 (WS5 / 2차년도 WS2 자동 승격·롤백의 기반).

target_col(예측 대상)마다 별도 디렉터리(data/model_registry/<target_col>/)를 사용해
"EV 충전수요"와 "EV 가용 V2G 자원" 등 복수 예측모델을 독립적으로 버전관리한다.
"""
import json
from pathlib import Path

import joblib
import pandas as pd

from src.paths import MODEL_REGISTRY_DIR

METADATA_FILE = "registry.json"


class ModelRegistry:
    def __init__(self, target_col: str, base_dir: Path | None = None):
        self.target_col = target_col
        self.dir = (base_dir or MODEL_REGISTRY_DIR) / target_col
        self._meta_path = self.dir / METADATA_FILE

    def _read_meta(self) -> dict:
        if not self._meta_path.exists():
            return {"active_version": None, "versions": {}}
        return json.loads(self._meta_path.read_text(encoding="utf-8"))

    def _write_meta(self, meta: dict) -> None:
        self._meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def active_version(self) -> int | None:
        return self._read_meta()["active_version"]

    def register(self, model, feature_cols: list[str], weeks: int, holdout_mae: float, holdout_rmse: float, n_train: int,
                 data_range: dict | None = None, data_source: str | None = None) -> int:
        """data_range: 학습·검증 구간(시작~끝 시각), data_source: 'synthetic'|'measured' — 같은 조건으로 재현하기 위한 기록."""
        self.dir.mkdir(parents=True, exist_ok=True)
        meta = self._read_meta()
        version = max((int(v) for v in meta["versions"]), default=0) + 1

        joblib.dump({"model": model, "feature_cols": feature_cols}, self.dir / f"v{version}.joblib")
        meta["versions"][str(version)] = {
            "version": version,
            "weeks": weeks,
            "holdout_mae": holdout_mae,
            "holdout_rmse": holdout_rmse,
            "n_train": n_train,
            "data_range": data_range,
            "data_source": data_source,
            "feature_count": len(feature_cols),
            "registered_at": pd.Timestamp.now(tz="Asia/Seoul").isoformat(timespec="seconds"),
        }
        meta["active_version"] = version
        self._write_meta(meta)
        return version

    def load(self, version: int | None = None):
        meta = self._read_meta()
        version = version if version is not None else meta["active_version"]
        if version is None:
            raise FileNotFoundError(f"등록된 모델이 없습니다: {self.target_col}")

        artifact = joblib.load(self.dir / f"v{version}.joblib")
        info = meta["versions"][str(version)]
        return artifact["model"], artifact["feature_cols"], info
