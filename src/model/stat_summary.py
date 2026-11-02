"""EV Bot에게 줄 "통계 요약본" — 시나리오 실행 이력(dashboard_run_history)을 모아
추세(절감률·예측오차가 나아지고 있는지)를 짧은 한국어 텍스트로 요약한다. 이력에 실제
있는 값만 쓰고 지어내지 않는다 — 이력이 부족하면 그 사실 자체를 명시한다.
"""
import pandas as pd

MIN_RUNS_FOR_TREND = 2


def _trend_word(delta: float, higher_is_better: bool) -> str:
    if abs(delta) < 1e-9:
        return "변화 없음"
    improved = (delta > 0) if higher_is_better else (delta < 0)
    return "개선" if improved else "악화"


def build_stat_summary(history: pd.DataFrame) -> str | None:
    """history: `read_dashboard_run_history()`가 반환한, 오래된 순 정렬된 DataFrame
    (같은 시나리오 규모로 필터링된 상태로 전달돼야 서로 다른 EV 대수 간 절감액을
    비교하는 오류를 피할 수 있다). 이력이 아예 없으면 None(EV Bot은 이 블록을
    생략하고 일반 답변으로 진행)."""
    if history.empty:
        return None

    latest = history.iloc[-1]
    if len(history) < MIN_RUNS_FOR_TREND:
        return (
            f"실행 이력이 {len(history)}건뿐이라 추세 비교는 아직 불가능합니다. "
            f"최근 실행({latest['generated_at']}): 절감률 {latest['savings_pct']:.1f}%, "
            f"예측 MAE {latest['forecast_mae']:.3f}."
        )

    prev = history.iloc[-2]
    first = history.iloc[0]
    savings_delta = latest["savings_pct"] - prev["savings_pct"]
    mae_delta = latest["forecast_mae"] - prev["forecast_mae"]
    savings_delta_vs_first = latest["savings_pct"] - first["savings_pct"]

    return "\n".join([
        f"최근 {len(history)}회 실행 이력 기준 통계 요약(동일 시나리오 규모: "
        f"EV {int(latest['n_ev'])}대·{int(latest['weeks'])}주):",
        f"- 최근 실행({latest['generated_at']}): 절감률 {latest['savings_pct']:.1f}%, "
        f"예측 MAE {latest['forecast_mae']:.3f}",
        f"- 직전 실행 대비: 절감률 {savings_delta:+.1f}%p({_trend_word(savings_delta, True)}), "
        f"예측 MAE {mae_delta:+.3f}({_trend_word(mae_delta, False)})",
        f"- 이력 중 첫 실행({first['generated_at']}) 대비 절감률 {savings_delta_vs_first:+.1f}%p",
    ])
