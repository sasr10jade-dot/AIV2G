"""CPO 어댑터·동기화, 계시별 요금표 테스트."""
import json
import sqlite3

import pandas as pd
import pytest

from src.cpo import sync
from src.cpo.adapters import CsvFileAdapter, MockAdapter, load_adapters
from src.cpo.base import CpoDataError
from src.grid import tariff as t


def _csv(tmp_path, rows, name="cpo_a.csv"):
    p = tmp_path / name
    pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8-sig")
    return p


MAP = {"session_id": "txn", "charger_id": "cid", "start_ts": "begin", "duration_h": "hours", "energy_kwh": "wh"}


def test_csv_adapter_maps_columns_and_converts_wh(tmp_path):
    p = _csv(tmp_path, [{"txn": "1", "cid": "C1", "begin": "2026-09-01 10:00", "hours": "2", "wh": "14000"}])
    df = CsvFileAdapter("CPO-A", p, MAP, energy_unit="wh").fetch_sessions()
    assert df.loc[0, "energy_kwh"] == 14.0 and df.loc[0, "zone"] == 0


def test_csv_adapter_reports_missing_mapped_column_and_bad_rows(tmp_path):
    p = _csv(tmp_path, [{"txn": "1", "cid": "C1", "begin": "2026-09-01 10:00", "hours": "2"}])
    with pytest.raises(CpoDataError, match="매핑된 컬럼"):
        CsvFileAdapter("CPO-A", p, MAP).fetch_sessions()
    p2 = _csv(tmp_path, [{"txn": "1", "cid": "C1", "begin": "not-a-date", "hours": "2", "wh": "5"}], "b.csv")
    with pytest.raises(CpoDataError, match="형식 오류"):
        CsvFileAdapter("CPO-A", p2, MAP).fetch_sessions()


def test_missing_file_returns_empty_not_error(tmp_path):
    assert CsvFileAdapter("CPO-A", tmp_path / "none.csv", MAP).fetch_sessions().empty


def test_mock_adapter_is_deterministic():
    a = MockAdapter("CPO-M", seed=3).fetch_sessions()
    assert a.equals(MockAdapter("CPO-M", seed=3).fetch_sessions()) and len(a) > 0


def test_sync_is_idempotent_isolated_per_operator_and_namespaced(tmp_path):
    con = sqlite3.connect(":memory:")
    bad = CsvFileAdapter("CPO-BAD", tmp_path / "x.csv", {"session_id": "nope"})
    _csv(tmp_path, [{"txn": "1"}], "x.csv")  # 필수 컬럼 없음 → 이 사업자만 실패
    good_a, good_b = MockAdapter("CPO-A", seed=1), MockAdapter("CPO-B", seed=1)
    r1 = sync.sync_all([bad, good_a, good_b], con=con)
    assert r1["ok"] is False
    status = {r["operator_id"]: r["status"] for r in r1["results"]}
    assert status == {"CPO-BAD": "error", "CPO-A": "ok", "CPO-B": "ok"}
    n = con.execute("SELECT COUNT(*) FROM charger_sessions").fetchone()[0]
    assert n == r1["results"][1]["rows"] + r1["results"][2]["rows"]  # 같은 시드여도 ID 충돌 없음
    sync.sync_all([good_a, good_b], con=con)  # 재동기화 → 중복 없음
    assert con.execute("SELECT COUNT(*) FROM charger_sessions").fetchone()[0] == n
    assert {r[0] for r in con.execute("SELECT DISTINCT operator_id FROM charger_sessions")} == {"CPO-A", "CPO-B"}


def test_load_adapters_from_config_skips_disabled(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"sources": [
        {"operator_id": "CPO-A", "type": "mock", "enabled": True},
        {"operator_id": "CPO-B", "type": "mock", "enabled": False},
    ]}), encoding="utf-8")
    assert [a.operator_id for a in load_adapters(cfg)] == ["CPO-A"]


# ------------------------------------------------------------------ 요금표 --

def test_default_tariffs_load_and_are_flagged_unverified():
    tariffs = t.load_tariffs()
    assert "kepco_ev_example" in tariffs and not tariffs["kepco_ev_example"].verified


def test_price_lookup_by_hour_and_series():
    tf = t.get_tariff("kepco_ev_example")
    hours, rates, name = tf._season(9)                       # 9월 = 봄가을(평가의견 3·11: 계절·시간대 단가)
    assert name == "spring_fall"
    assert tf.price_at("2026-09-01 03:30") == rates["light"]
    assert tf.price_at("2026-09-01 12:00") == rates[hours[12]]
    s = tf.price_series(pd.date_range("2026-09-01", periods=24, freq="h"))
    assert len(s) == 24 and set(s) <= set(rates.values())


def test_tariff_seasons_change_price_for_same_hour():
    tf = t.get_tariff("kepco_ev_example")
    assert {tf.season_at("2026-07-15"), tf.season_at("2026-04-15"), tf.season_at("2026-12-15")} == {"summer", "spring_fall", "winter"}
    assert tf.price_at("2026-07-15 14:00") != tf.price_at("2026-04-15 14:00")    # 같은 시각이라도 계절별 단가가 다르다


def test_2026_reform_bands_day_rules_and_weekend_discount():
    """2026-04-16 개편 잠정 적용: 여름 18~21시 최대, 11~15시 중간, 토요일 최대→중간, 일·공휴일 경부하,
    봄가을 주말 11~14시 50% — 보도된 할인액(토 48.6·일 42.7원)과 일치."""
    tf = t.get_tariff("kepco_ev_example")
    assert tf.band_at("2026-07-15 19:00") == "peak" and tf.band_at("2026-07-15 11:00") == "mid"   # 수요일
    assert tf.band_at("2026-07-18 19:00") == "mid" and tf.band_at("2026-07-19 19:00") == "light"  # 토·일
    assert tf.price_at("2026-10-10 12:00") == pytest.approx(48.6) and tf.price_at("2026-10-11 12:00") == pytest.approx(42.7)
    assert tf.price_at("2026-10-14 12:00") == pytest.approx(97.2)                                 # 평일은 할인 없음
    assert tf.price_at("2026-10-14 19:00", holiday=True) == pytest.approx(85.4)                   # 평일 공휴일 = 경부하
    old = t.get_tariff("kepco_ev_2023")
    assert old.band_at("2026-07-15 19:00") == "mid" and old.price_at("2026-10-10 12:00") == old.price_at("2026-10-14 12:00")


def test_invalid_tariff_rejected(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"tariffs": {"x": {"hours": ["a"] * 23, "rates": {"a": 1}}}}), encoding="utf-8")
    with pytest.raises(t.TariffError, match="24칸"):
        t.load_tariffs(p)
    p.write_text(json.dumps({"tariffs": {"x": {"hours": ["a"] * 24, "rates": {"b": 1}}}}), encoding="utf-8")
    with pytest.raises(t.TariffError, match="없는 구간명"):
        t.load_tariffs(p)
    with pytest.raises(t.TariffError, match="알 수 없는"):
        t.get_tariff("nope")
