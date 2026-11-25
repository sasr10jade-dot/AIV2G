"""일 배치 — 계획서의 "예측 → 최적화 → 추천" 운영 구조를 매일 한 번 돌린다(AI 지속 업무 C1·C2·C3·C4).

순서(한 단계가 실패해도 다음 단계는 계속하고, 실패는 웹앱 관리 > 예외 기록에 남긴다)
  C4 데이터 결합 : 실증 데이터셋(1건 = 이벤트 × [① 차량·충전기 + ② AI 학습 블록])을 다시 만들어 건수·결합 성공률을 남긴다.
  C1 예측 갱신   : 충전수요·가용 배터리의 다음 48시간 전망(아크릴 예측이 있으면 그것, 없으면 기준 모델).
  C3 오차 계산   : 아크릴 예측이 수신돼 있으면 같은 시각 실측과 비교한다(수신 자체는 아크릴이 API로 밀어 넣는다).
  C2 계획·추천   : 승인된 서포터즈 차량마다 다음 출발까지의 충전(·방전) 계획을 만들어 앱 추천으로 발행한다.
                   문자·푸시 같은 외부 발송은 하지 않는다 — 앱 알림함/추천 기록에만 남긴다.

결과: data/batch/daily/<날짜>/ 에 forecast.json·plans.json·dataset_summary.json·dataset.csv(차량 ID 가명),
      data/batch/daily_history.jsonl 에 실행 요약 한 줄.
실측 데이터가 없으면 데모(합성) 시나리오로 돌고, 결과의 data_source가 "synthetic"으로 남는다.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.paths import DATA_DIR

logger = logging.getLogger(__name__)

BATCH_DIR = DATA_DIR / "batch" / "daily"
HISTORY = DATA_DIR / "batch" / "daily_history.jsonl"
FORECAST_TARGETS = ("ev_charging_demand_kw", "ev_available_v2g_kwh")
DEFAULT_SOC_PCT = 50.0      # 최근 관측 SOC가 없는 차량(앱 가입만 하고 OBD가 아직 없는 차량)의 가정값
DEFAULT_NEED_SOC_PCT = 80.0


def _record_issue(stage: str, message: str, detail: dict | None = None) -> None:
    from src.automation import issues
    issues.record(stage, message, detail=detail)


def _step(name: str, stage: str, fn, out: dict) -> object:
    """단계 하나를 실행하고 소요 시간·성공 여부를 out["steps"]에 남긴다. 실패해도 예외를 밖으로 던지지 않는다."""
    t0 = time.monotonic()
    try:
        result = fn()
        out["steps"].append({"step": name, "ok": True, "seconds": round(time.monotonic() - t0, 1)})
        return result
    except Exception as exc:   # noqa: BLE001 — 한 단계 실패가 배치 전체를 멈추지 않게
        logger.exception("일 배치 %s 단계 실패", name)
        out["steps"].append({"step": name, "ok": False, "seconds": round(time.monotonic() - t0, 1),
                             "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        _record_issue(stage, f"일 배치 {name} 단계 실패: {type(exc).__name__}", {"error": str(exc)[:300]})
        return None


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


# ------------------------------------------------------------------ C4 데이터 결합 --

def build_daily_dataset(ev_df, charger_df, grid_df, day_dir: Path) -> dict:
    from src.pipeline import dataset
    from src.privacy.pseudonym import pseudo_id

    ds = dataset.build_dataset(ev_df, charger_df, grid_df)
    summary = dataset.summarize(ds)
    summary.pop("plan", None)
    _write_json(day_dir / "dataset_summary.json", summary)
    if not ds.empty:
        out = ds.copy()
        out["ev_id"] = out["ev_id"].map(lambda v: pseudo_id("ev", str(v)) if isinstance(v, str) and v else None)
        out.to_csv(day_dir / "dataset.csv", index=False, encoding="utf-8-sig")
    if summary.get("join_rate_ok") is False:
        _record_issue("ingest", f"결합 성공률 {summary['join_rate_pct']}% — 기준 {summary['min_join_rate_pct']}% 미달",
                      {"counted": summary["counted"], "events": summary["events"]})
    return {"events": summary["events"], "counted": summary["counted"], "join_rate_pct": summary["join_rate_pct"],
            "join_rate_ok": summary["join_rate_ok"],
            "by_kind": {k: v["counted"] for k, v in summary["by_kind"].items()}}


# ------------------------------------------------------------------ C1 예측 갱신 --

def refresh_forecast(day_dir: Path) -> dict:
    from src.model import forecast_service

    result, brief = {}, {}
    for target in FORECAST_TARGETS:
        fc = forecast_service.forecast_preferred(target, "hourly")
        result[target] = fc
        vals = fc.get("values") or []
        peak_i = max(range(len(vals)), key=vals.__getitem__) if vals else None
        brief[target] = {
            "model_source": fc.get("model_source", "baseline"), "model_version": fc.get("model_version"),
            "hours": len(vals), "total": round(float(sum(vals)), 1) if vals else None,
            "peak_at": fc["timestamps"][peak_i] if peak_i is not None else None,
            "peak_value": vals[peak_i] if peak_i is not None else None,
        }
    _write_json(day_dir / "forecast.json", result)
    return brief


# ------------------------------------------------------------------ C3 오차 계산 --

def acryl_error_check(city_hourly: pd.DataFrame) -> dict:
    from src.acryl import link

    status = {s["target"]: s for s in link.forecast_status()}
    out = {}
    for target in FORECAST_TARGETS:
        if not (status.get(target) or {}).get("batches"):
            out[target] = {"received": False}
            continue
        out[target] = {"received": True, **link.evaluate_against_actual(target, city_hourly)}
    return out


# ------------------------------------------------------------------ C2 계획·추천 --

def _latest_soc(ev_df: pd.DataFrame) -> dict:
    if ev_df is None or ev_df.empty or "soc_pct" not in ev_df.columns:
        return {}
    last = ev_df.dropna(subset=["soc_pct"]).sort_values("timestamp").groupby("ev_id")["soc_pct"].last()
    return {str(k): float(v) for k, v in last.items()}


def plan_and_recommend(ev_df: pd.DataFrame, day_dir: Path, now: datetime | None = None, issue: bool = True) -> dict:
    """승인된 서포터즈 차량마다 충전(·방전) 계획을 만들고 앱 추천으로 기록한다."""
    from src.advisory import owner_guide, service as advisory
    from src.privacy import consent
    from src.privacy.pseudonym import pseudo_id
    from src.registry import service as registry

    soc = _latest_soc(ev_df)
    members = [r for r in registry.list_all("approved", limit=1000) if r.get("ev_id")]
    plans, issued, failed = [], 0, 0
    for reg in members:
        ev_id = reg["ev_id"]
        row = {"ev_id": pseudo_id("ev", ev_id)}
        try:
            profile = owner_guide.vehicle_profile(reg["username"], ev_id)
            cur = soc.get(ev_id)
            row["soc_source"] = "obd" if cur is not None else "assumed"
            dep = owner_guide.next_departure(reg.get("departure_time"), now)
            inputs = owner_guide.GuideInputs(cur if cur is not None else DEFAULT_SOC_PCT,
                                             float(reg.get("target_soc_pct") or DEFAULT_NEED_SOC_PCT), dep,
                                             owner_guide.DEFAULT_COMPENSATION_ID, reg.get("min_soc_pct"))
            participating = consent.has_consent(ev_id, "v2g_discharge")
            guide = owner_guide.build_guide(profile, inputs, now=now, participating=participating)
            plan = guide.get("plan") or {}
            safety = guide.get("safety") or {}
            row.update({"period": guide.get("period"), "charge": plan.get("charge", []), "discharge": plan.get("discharge", []),
                        "safety_passed": safety.get("passed"), "participating": participating})
            if issue:
                row["advisory_id"] = advisory.issue(ev_id, {
                    "charge": plan.get("charge", []), "discharge": plan.get("discharge", []), "period": guide.get("period"),
                    "assumptions": {"verified": bool(guide.get("verified"))}},
                    channel="app", inputs={**(guide.get("inputs") or {}), "source": "daily_batch", "soc_source": row["soc_source"]})
                issued += 1
        except Exception as exc:   # noqa: BLE001 — 한 차량 실패가 다른 차량을 막지 않게
            failed += 1
            row["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
        plans.append(row)
    _write_json(day_dir / "plans.json", plans)
    if failed:
        _record_issue("forecast", f"일 배치 충전 계획 {failed}대 실패", {"vehicles": len(members)})
    return {"vehicles": len(members), "issued": issued, "failed": failed,
            "soc_assumed": sum(1 for p in plans if p.get("soc_source") == "assumed")}


# ------------------------------------------------------------------ 전체 --

def run_daily(now: datetime | None = None, issue: bool = True) -> dict:
    from src.datagen.config import SimConfig
    from src.pipeline import source
    from src.pipeline.store import build_city_hourly

    started = datetime.now(timezone.utc)
    day = (now or started.astimezone()).strftime("%Y-%m-%d")
    day_dir = BATCH_DIR / day
    out: dict = {"started_at": started.isoformat(timespec="seconds"), "day": day, "steps": []}

    cfg = SimConfig()
    loaded = _step("load", "ingest", lambda: source.load_scenario(cfg), out)
    if loaded is None:
        out["data_source"] = None
    else:
        ev_df, charger_df, grid_df, _real = loaded
        out["data_source"] = "measured" if source.provenance().get("ev_telemetry") == "measured" else "synthetic"
        out["dataset"] = _step("dataset", "ingest", lambda: build_daily_dataset(ev_df, charger_df, grid_df, day_dir), out)
        out["forecast"] = _step("forecast", "forecast", lambda: refresh_forecast(day_dir), out)
        city = _step("city_hourly", "ingest", lambda: build_city_hourly(ev_df, grid_df, cfg, charger_df=charger_df), out)
        if city is not None:
            out["acryl"] = _step("acryl_error", "forecast", lambda: acryl_error_check(city), out)
        out["recommend"] = _step("recommend", "forecast", lambda: plan_and_recommend(ev_df, day_dir, now, issue), out)

    out["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out["ok"] = all(s["ok"] for s in out["steps"])
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
    return out


def history(limit: int = 30) -> list[dict]:
    if not HISTORY.exists():
        return []
    lines = HISTORY.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))
