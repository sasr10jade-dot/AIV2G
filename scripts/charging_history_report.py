"""충전사업자 충전 내역(xlsx)을 집계해 요약 문서·충전 세션 CSV로 바꾼다 — 개인 식별 값은 쓰지 않는다.

입력 파일의 '인증번호'(회원 식별 가능)·승인번호·결제 관련 컬럼은 읽지도 저장하지도 않는다. 출력은 두 가지다.
  1) 집계 요약(JSON·Markdown) — 건수, 기간, 충전량·시간 분포, 시작 시각대, 판매 단가, 유휴(꽂아 둔) 시간 비율
  2) 충전 세션 CSV(선택) — 시스템 `charger_sessions` 형식에 맞춘 비식별 데이터(ev_id 없음). 업로드는 하지 않는다.

사용:
  python scripts/charging_history_report.py "충전 내역 목록.xlsx" --out output/charging_history
  python scripts/charging_history_report.py 파일.xlsx --out 폴더 --sessions-csv
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

USED_COLS = ["충전기용량", "충전소명", "충전시작일시", "충전종료일시", "충전량(kWh)", "충전금액", "SID", "CID", "ConnID"]


def load(path: Path) -> pd.DataFrame:
    raw = pd.read_excel(path, header=None)
    header_row = next(i for i in range(min(15, len(raw))) if "충전시작일시" in raw.iloc[i].astype(str).tolist())
    df = pd.read_excel(path, header=header_row, usecols=lambda c: c in USED_COLS)   # 식별·결제 컬럼은 아예 읽지 않는다
    df = df.dropna(subset=["충전시작일시", "충전종료일시"])
    df["start"] = pd.to_datetime(df["충전시작일시"])
    df["end"] = pd.to_datetime(df["충전종료일시"])
    df["kwh"] = pd.to_numeric(df["충전량(kWh)"], errors="coerce")
    df["amount"] = pd.to_numeric(df["충전금액"], errors="coerce")
    df["capacity_kw"] = pd.to_numeric(df["충전기용량"], errors="coerce")
    df["hours"] = (df["end"] - df["start"]).dt.total_seconds() / 3600
    return df[(df["hours"] > 0) & (df["kwh"] > 0)].reset_index(drop=True)


def summarize(df: pd.DataFrame) -> dict:
    cap = float(df["capacity_kw"].median())
    need_h = df["kwh"] / cap                              # 정격 출력으로 충전했다면 걸렸을 시간
    idle = (1 - need_h / df["hours"]).clip(lower=0)      # 꽂아 둔 시간 중 실제로 충전하지 않은 비율(근사)
    price = df["amount"] / df["kwh"]
    q = lambda s, p: round(float(s.quantile(p)), 2)
    return {
        "sessions": int(len(df)),
        "period": [df["start"].min().strftime("%Y-%m-%d"), df["start"].max().strftime("%Y-%m-%d")],
        "stations": sorted(df["충전소명"].dropna().unique().tolist()),
        "charger_kw": cap,
        "chargers": int(df[["SID", "CID", "ConnID"]].drop_duplicates().shape[0]),
        "by_month": {k: int(v) for k, v in df["start"].dt.strftime("%Y-%m").value_counts().sort_index().items()},
        "energy_kwh": {"total": round(float(df["kwh"].sum()), 1), "mean": round(float(df["kwh"].mean()), 1), "p25": q(df["kwh"], .25), "median": q(df["kwh"], .5), "p75": q(df["kwh"], .75), "max": round(float(df["kwh"].max()), 1)},
        "plugged_hours": {"mean": round(float(df["hours"].mean()), 2), "median": q(df["hours"], .5), "min": round(float(df["hours"].min()), 2), "max": round(float(df["hours"].max()), 2)},
        "start_hour_counts": {int(k): int(v) for k, v in df["start"].dt.hour.value_counts().sort_index().items()},
        "daytime_start_share_8_18": round(float(df["start"].dt.hour.between(8, 18).mean()), 3),
        "price_krw_per_kwh": {"mean": round(float(price.mean()), 1), "min": round(float(price.min()), 1), "max": round(float(price.max()), 1)},
        "idle_share_of_plugged_time": {"mean": round(float(idle.mean()), 3), "median": q(idle, .5)},
        "shiftable_note": "꽂아 둔 시간 중 실제 충전에 필요한 시간(충전량÷정격 출력)을 뺀 비율. 근사치이며 충전 곡선·출력 제한은 반영하지 않았다.",
    }


def to_sessions(df: pd.DataFrame) -> pd.DataFrame:
    """시스템 charger_sessions 형식(비식별). session_id는 시작 시각·충전기로 만든 해시라 원본으로 되돌릴 수 없다."""
    sid = [hashlib.sha256(f"{r.SID}|{r.CID}|{r.start.isoformat()}".encode()).hexdigest()[:12] for r in df.itertuples()]
    return pd.DataFrame({
        "session_id": ["real-" + x for x in sid], "charger_id": df["CID"].astype(str).radd("CH-"), "zone": 0,
        "start_ts": df["start"], "duration_h": df["hours"].round(2), "energy_kwh": df["kwh"].round(2), "ev_id": "", "operator_id": "real", "source": "operator_export",
    })


def markdown(s: dict) -> str:
    h = s["start_hour_counts"]
    lines = [
        "# 충전 내역 집계 요약 (식별정보 제외)", "",
        f"- 건수 **{s['sessions']}건** · 기간 {s['period'][0]} ~ {s['period'][1]} · 충전소 {', '.join(s['stations'])} · 정격 {s['charger_kw']:.0f}kW · 충전기 {s['chargers']}기",
        f"- 충전량: 평균 {s['energy_kwh']['mean']}kWh (중앙 {s['energy_kwh']['median']}, 사분위 {s['energy_kwh']['p25']}~{s['energy_kwh']['p75']}, 최대 {s['energy_kwh']['max']})",
        f"- 꽂아 둔 시간: 평균 {s['plugged_hours']['mean']}시간 (중앙 {s['plugged_hours']['median']}, {s['plugged_hours']['min']}~{s['plugged_hours']['max']})",
        f"- 낮 시간(8~18시) 시작 비율 {s['daytime_start_share_8_18']*100:.0f}%",
        f"- 판매 단가: 평균 {s['price_krw_per_kwh']['mean']}원/kWh ({s['price_krw_per_kwh']['min']}~{s['price_krw_per_kwh']['max']})",
        f"- 꽂아 둔 시간 중 실제 충전이 아닌 시간 비율(근사): 평균 {s['idle_share_of_plugged_time']['mean']*100:.0f}% — {s['shiftable_note']}", "",
        "## 월별 건수", "", "| 월 | 건수 |", "|---|---|", *[f"| {k} | {v} |" for k, v in s["by_month"].items()], "",
        "## 시작 시각대", "", "| 시 | 건수 |", "|---|---|", *[f"| {k:02d} | {v} |" for k, v in h.items()], "",
        "주의: 한 곳·충전기 1기의 기록이라 일반화하기 어렵고, 결제 상태가 모두 '결제예외'여서 금액이 실제 결제 값인지는 확인이 필요하다.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("--out", type=Path, default=Path("output/charging_history"))
    ap.add_argument("--sessions-csv", action="store_true", help="charger_sessions 형식 비식별 CSV도 만든다(업로드는 하지 않음)")
    args = ap.parse_args()
    df = load(args.xlsx)
    s = summarize(df)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "summary.md").write_text(markdown(s), encoding="utf-8")
    print(markdown(s))
    if args.sessions_csv:
        to_sessions(df).to_csv(args.out / "charger_sessions_real.csv", index=False, encoding="utf-8")
        print(f"세션 CSV: {args.out / 'charger_sessions_real.csv'} (업로드는 별도 승인 후)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
