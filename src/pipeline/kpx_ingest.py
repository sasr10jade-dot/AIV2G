"""한국전력거래소(KPX) 계통한계가격(SMP) 오픈API 연동 — 공공데이터포털 등록 API
(https://www.data.go.kr/data/15076302/openapi.do)를 통해 `grid_signal.price_krw_per_kwh`를
합성값 대신 실제 SMP로 교체한다.

**중요한 스코프 제한**: 이 프로젝트는 "광주/전남 도시형" 에너지 모델이라 지역 단위
전력수요가 필요하지만, SMP는 원래 **전국 단일 정산가격**(육지/제주 구분만 있음)이라
지역 스케일 문제가 없다 — 이전 분석(e-policy.or.kr 실시간 수급 데이터)에서 발견된
"전국 76,062MW vs 도시 모델 120MW" 같은 600배 스케일 불일치가 SMP에는 애초에
적용되지 않는다. 그래서 `power_demand_mw`/`renewable_gen_mw`는 여전히 합성값을
쓰고, `price_krw_per_kwh` 하나만 이 모듈로 실측 대체한다.

**API 키 필요**: `KPX_SERVICE_KEY` 환경변수(.env)에 공공데이터포털에서 발급받은
인증키를 넣어야 동작한다 — 이 세션에서는 실제 키가 없어 fetch 함수를 라이브로
검증하지 못했다(`test_kpx_ingest.py`는 문서화된 응답 스펙으로 만든 샘플 XML로
파싱 로직만 검증한다). 태양광 발전량 API(15103243, 지역별·시간별)는 상세 요청/응답
필드 명세가 Swagger 위젯으로 렌더링돼 이번엔 확인하지 못해 연동 코드를 만들지
않았다 — 재확인 후 별도 작업 필요.
"""
from __future__ import annotations

import os
from xml.etree import ElementTree

import pandas as pd
import requests

KPX_SMP_URL = "https://openapi.kpx.or.kr/openapi/smp1hToday/getSmp1hToday"
AREA_CODES = {"mainland": "1", "jeju": "9"}


class KpxNotConfigured(Exception):
    """KPX_SERVICE_KEY가 설정되지 않은 경우."""


class KpxApiError(Exception):
    """KPX가 resultCode != '00'으로 응답한 경우(잘못된 키, 승인 대기 등)."""


def parse_smp_xml(xml_bytes: bytes) -> pd.DataFrame:
    """SMP 응답 XML을 (timestamp, price_krw_per_kwh) 데이터프레임으로 변환한다.

    data.go.kr에 문서화된 응답 필드(resultCode/resultMsg/tradeDay/tradHour/areaCd/smp)를
    그대로 반영한다 — 실제 라이브 응답 샘플은 이번에 구하지 못해 문서 명세 기준으로
    작성했다(키 발급 후 실호출로 재검증 필요)."""
    root = ElementTree.fromstring(xml_bytes)

    result_code = root.findtext(".//resultCode")
    result_msg = root.findtext(".//resultMsg", default="")
    if result_code != "00":
        raise KpxApiError(f"KPX API 오류 (resultCode={result_code}): {result_msg}")

    rows = []
    for item in root.iter("item"):
        trade_day = item.findtext("tradeDay")
        trad_hour = item.findtext("tradHour")
        smp = item.findtext("smp")
        if trade_day is None or trad_hour is None or smp is None:
            continue
        ts = pd.to_datetime(trade_day, format="%Y%m%d") + pd.Timedelta(hours=int(trad_hour))
        rows.append({"timestamp": ts, "price_krw_per_kwh": float(smp)})

    return pd.DataFrame(rows, columns=["timestamp", "price_krw_per_kwh"]).sort_values("timestamp").reset_index(drop=True)


def fetch_smp_hourly(area: str = "mainland", service_key: str | None = None, timeout: float = 10.0) -> pd.DataFrame:
    """오늘자 시간별 SMP를 실시간으로 조회한다. `KPX_SERVICE_KEY` 환경변수(또는
    `service_key` 인자)가 필요 — 없으면 실제 네트워크 호출 없이 즉시 실패한다."""
    key = service_key or os.environ.get("KPX_SERVICE_KEY")
    if not key:
        raise KpxNotConfigured(
            "KPX_SERVICE_KEY가 설정되지 않았습니다. 공공데이터포털(data.go.kr)에서 "
            "'한국전력거래소_계통한계가격조회' API 활용신청 후 발급받은 인증키를 "
            ".env 파일에 넣어주세요 — 개발단계도 심의승인이 필요해 즉시 발급되지 않을 수 있습니다."
        )
    if area not in AREA_CODES:
        raise ValueError(f"알 수 없는 지역입니다: {area} (선택지: {', '.join(AREA_CODES)})")

    resp = requests.get(
        KPX_SMP_URL, params={"ServiceKey": key, "areaCd": AREA_CODES[area]}, timeout=timeout,
    )
    resp.raise_for_status()
    return parse_smp_xml(resp.content)


def merge_smp_into_grid_signal(grid_df: pd.DataFrame, smp_df: pd.DataFrame) -> pd.DataFrame:
    """기존 `grid_signal` 데이터프레임(합성이든 실측이든)의 `price_krw_per_kwh`를,
    시각이 일치하는 시간대만 실측 SMP 값으로 덮어쓴다. `power_demand_mw`/
    `renewable_gen_mw`/`temperature_c`/`is_holiday`는 이 함수의 책임이 아니다 —
    SMP 하나만으로는 나머지 필드를 만들 근거가 없어(전국 단일가라 지역 부하와
    무관) 그대로 둔다."""
    merged = grid_df.copy()
    price_by_ts = dict(zip(smp_df["timestamp"], smp_df["price_krw_per_kwh"]))
    mask = merged["timestamp"].isin(price_by_ts)
    merged.loc[mask, "price_krw_per_kwh"] = merged.loc[mask, "timestamp"].map(price_by_ts)
    return merged
