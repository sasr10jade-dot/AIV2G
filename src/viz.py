"""결과 시각화 — 보고서/PPT에 바로 쓸 수 있는 PNG 차트 저장.

dataviz 스킬 가이드에 따라 색상은 시맨틱하게 고정(무제어=copper, AI 최적화=teal)하고,
직접 라벨링·엷은 그리드·여백을 신경써서 프레젠테이션용으로 바로 쓸 수 있게 만든다.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

from src.paths import OUTPUT_DIR

INK = "#101820"
MUTED = "#7C8A93"
LINE = "#DCE3E7"
BASELINE_COLOR = "#D2691E"   # copper — 무제어
OPTIMIZED_COLOR = "#00897B"  # teal — AI 최적화

plt.rcParams.update({
    "font.family": "Malgun Gothic",
    "axes.edgecolor": LINE,
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.unicode_minus": False,
})


def plot_poc_comparison(comparison_df: pd.DataFrame, out_name: str = "poc_load_curve.png") -> str:
    """일반충전(baseline) vs AI 최적화(optimized) 부하곡선 비교 차트."""
    df = comparison_df.copy().sort_values("timestamp")
    # 시각적 과밀 방지를 위해 마지막 5일만 표시
    window = df[df["timestamp"] >= df["timestamp"].max() - pd.Timedelta(days=5)]

    fig, ax = plt.subplots(figsize=(11, 4.2), dpi=150)
    ax.fill_between(window["timestamp"], window["baseline_kw"], color=BASELINE_COLOR, alpha=0.15, step="mid")
    ax.plot(window["timestamp"], window["baseline_kw"], color=BASELINE_COLOR, linewidth=2,
            label="일반 충전(무제어)", drawstyle="steps-mid")
    ax.fill_between(window["timestamp"], window["optimized_kw"], color=OPTIMIZED_COLOR, alpha=0.15, step="mid")
    ax.plot(window["timestamp"], window["optimized_kw"], color=OPTIMIZED_COLOR, linewidth=2,
            label="AI-V2G 최적화", drawstyle="steps-mid")

    base_peak = window["baseline_kw"].max()
    opt_peak = window["optimized_kw"].max()
    ax.axhline(base_peak, color=BASELINE_COLOR, linewidth=0.8, linestyle="--", alpha=0.6)
    ax.axhline(opt_peak, color=OPTIMIZED_COLOR, linewidth=0.8, linestyle="--", alpha=0.6)
    ax.text(window["timestamp"].iloc[-1], base_peak, f" 피크 {base_peak:.0f}kW",
            color=BASELINE_COLOR, va="bottom", fontsize=9)
    ax.text(window["timestamp"].iloc[-1], opt_peak, f" 피크 {opt_peak:.0f}kW",
            color=OPTIMIZED_COLOR, va="bottom", fontsize=9)

    ax.set_title("EV 집계 충전부하 — 일반 충전 vs AI-V2G 최적화 (최근 5일)", fontsize=12, weight="bold", loc="left")
    ax.set_ylabel("kW")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
    fig.autofmt_xdate()
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=LINE, linewidth=0.7)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper right")
    fig.tight_layout()

    out_path = OUTPUT_DIR / out_name
    fig.savefig(out_path)
    plt.close(fig)
    return str(out_path)


def plot_forecast_fit(test_df: pd.DataFrame, target_label: str, out_name: str) -> str:
    """예측모델의 실측값 vs 예측값 비교 차트 (테스트 구간 앞 5일)."""
    df = test_df.copy().sort_values("timestamp")
    window = df[df["timestamp"] < df["timestamp"].min() + pd.Timedelta(days=5)]

    fig, ax = plt.subplots(figsize=(11, 3.6), dpi=150)
    ax.plot(window["timestamp"], window["y_true"], color=INK, linewidth=1.6, label="실측값")
    ax.plot(window["timestamp"], window["y_pred"], color=OPTIMIZED_COLOR, linewidth=1.6,
            linestyle="--", label="AI 예측값")
    ax.set_title(f"{target_label} — 실측 vs AI 예측 (홀드아웃 구간 앞 5일)", fontsize=12, weight="bold", loc="left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
    fig.autofmt_xdate()
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=LINE, linewidth=0.7)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper right")
    fig.tight_layout()

    out_path = OUTPUT_DIR / out_name
    fig.savefig(out_path)
    plt.close(fig)
    return str(out_path)
