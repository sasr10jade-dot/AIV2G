import numpy as np
import pytest

from src.datagen.config import SimConfig


@pytest.fixture(autouse=True)
def _isolated_privacy(tmp_path, monkeypatch):
    """동의·감사로그·가명키가 테스트 중 실제 data/ 폴더를 건드리지 않도록 격리한다."""
    from src.pipeline import store
    from src.privacy import consent, pseudonym, vault

    monkeypatch.setattr(store, "CURATED_DB", tmp_path / "curated.db")
    monkeypatch.setattr(consent, "PRIVACY_DB", tmp_path / "privacy.db")
    monkeypatch.setattr(pseudonym, "KEY_FILE", tmp_path / ".pseudo_key")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / ".pii_key")   # 개인정보 암호화 키도 data/에 만들지 않게
    monkeypatch.delenv("PII_KEY", raising=False)                    # 개발자 환경변수가 테스트 키를 덮지 않게


@pytest.fixture
def small_cfg() -> SimConfig:
    """빠른 통합테스트용 축소 규모 설정 (EV 4대, 충전기 6기, 2주)."""
    return SimConfig(
        n_ev=4, weeks=2, n_chargers=6, charger_days=14, n_zones=2, random_seed=7,
    )


@pytest.fixture
def medium_cfg() -> SimConfig:
    """모델 학습 테스트용 — 홀드아웃(2주) 이후에도 학습 데이터가 남도록 6주 규모."""
    return SimConfig(
        n_ev=6, weeks=6, n_chargers=10, charger_days=42, n_zones=2, random_seed=7,
    )


@pytest.fixture
def rng():
    return np.random.default_rng(7)


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    """로그인 시도 제한은 메모리 상태라, 테스트끼리 영향이 없도록 매번 비운다."""
    from src.auth import throttle

    throttle.reset()
    yield
    throttle.reset()
