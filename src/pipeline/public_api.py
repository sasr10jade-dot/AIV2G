"""공공데이터 API 커넥터 공통부 — 설정(config/public_apis.json) 기반 호출과 JSON/XML 응답 파싱.

API마다 요청 주소·파라미터 이름·응답 필드 이름이 다르고, 공개 페이지만으로는 확인되지 않는 값이 있다. 그래서 코드에
이름을 박아 두지 않고 설정으로 분리했다. 명세를 받으면 설정만 채우면 되고, 응답이 JSON이든 XML이든 같은 DataFrame으로
돌려준다. `spec_verified=false`인 동안은 호출 결과를 '검증 전'으로 표기해야 한다.

- 인증키는 설정 파일에 두지 않고 `key_env` 환경변수에서만 읽는다.
- 키가 없거나 엔드포인트가 비어 있으면 네트워크 호출 없이 바로 이유를 알려 준다.
- 응답 코드(`code_path`)가 `ok_codes`가 아니면 오류로 올린다(잘못된 키·한도 초과 등).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd
import requests

from src.paths import PROJECT_ROOT

PUBLIC_API_CONFIG = PROJECT_ROOT / "config" / "public_apis.json"


class ApiNotConfigured(Exception):
    """인증키 또는 엔드포인트(명세)가 아직 없음."""


class ApiResponseError(Exception):
    """응답 코드가 정상이 아니거나 형식을 해석할 수 없음."""


@dataclass(frozen=True)
class ApiSpec:
    api_id: str
    label: str
    endpoint: str | None
    key_env: str
    params: dict
    items_path: tuple[str, ...]
    code_path: tuple[str, ...]
    ok_codes: tuple[str, ...]
    fields: dict
    hour_base: int
    spec_verified: bool
    unit: str
    granularity: str
    dataset_url: str

    def status(self) -> dict:
        """키·명세 준비 상태(값은 노출하지 않는다) — 연동 현황 화면용."""
        return {
            "api_id": self.api_id, "label": self.label, "dataset_url": self.dataset_url,
            "endpoint_configured": bool(self.endpoint), "key_configured": bool(os.environ.get(self.key_env)),
            "key_env": self.key_env, "spec_verified": self.spec_verified, "unit": self.unit, "granularity": self.granularity,
        }


def load_spec(api_id: str, path: Path | None = None) -> ApiSpec:
    cfg = json.loads(Path(path or PUBLIC_API_CONFIG).read_text(encoding="utf-8"))
    item = cfg.get("apis", {}).get(api_id)
    if item is None:
        raise KeyError(f"알 수 없는 공공 API: {api_id}")
    return ApiSpec(
        api_id=api_id, label=item.get("label", api_id), endpoint=item.get("endpoint"), key_env=item["key_env"],
        params=item["params"], items_path=tuple(item["items_path"]), code_path=tuple(item.get("code_path", ())),
        ok_codes=tuple(str(c) for c in item.get("ok_codes", ())), fields=item["fields"],
        hour_base=int(item.get("hour_base", 0)), spec_verified=bool(item.get("spec_verified", False)),
        unit=item.get("unit", ""), granularity=item.get("granularity", ""), dataset_url=item.get("dataset_url", ""),
    )


def _dig(node, path: tuple[str, ...]):
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _xml_to_obj(elem: ElementTree.Element):
    """XML 요소를 JSON과 같은 dict/list 구조로 바꾼다 — 같은 경로 탐색 코드로 둘 다 처리하기 위함."""
    children = list(elem)
    if not children:
        return (elem.text or "").strip()
    out: dict = {}
    for child in children:
        value = _xml_to_obj(child)
        if child.tag in out:
            if not isinstance(out[child.tag], list):
                out[child.tag] = [out[child.tag]]
            out[child.tag].append(value)
        else:
            out[child.tag] = value
    return out


def _decode(content: bytes | str):
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ApiResponseError(f"JSON 해석 실패: {exc}") from exc
    if stripped.startswith("<"):
        try:
            root = ElementTree.fromstring(stripped)
        except ElementTree.ParseError as exc:
            raise ApiResponseError(f"XML 해석 실패: {exc}") from exc
        return {root.tag: _xml_to_obj(root)}
    raise ApiResponseError("JSON도 XML도 아닌 응답입니다")


def parse_items(content: bytes | str, spec: ApiSpec) -> list[dict]:
    """응답에서 응답 코드를 확인하고 항목 목록을 꺼낸다(항목이 하나여도 목록으로)."""
    obj = _decode(content)
    if spec.code_path and spec.ok_codes:
        code = _dig(obj, spec.code_path)
        if code is None or str(code) not in spec.ok_codes:
            raise ApiResponseError(f"API 응답 코드 오류(코드={code!r}, 기대={list(spec.ok_codes)})")
    items = _dig(obj, spec.items_path)
    if items in (None, ""):
        return []
    return items if isinstance(items, list) else [items]


def _number(value) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return float("nan")


def items_to_frame(items: list[dict], spec: ApiSpec) -> pd.DataFrame:
    """항목을 (timestamp, region, value) 표로 바꾼다. 거래시간은 hour_base 기준(1이면 1→00시)으로 맞춘다."""
    f = spec.fields
    rows = []
    for it in items:
        try:
            day = pd.to_datetime(str(it[f["date"]]).strip(), format="mixed")
            hour = int(float(str(it[f["hour"]]).strip())) - spec.hour_base
        except (KeyError, ValueError, TypeError) as exc:
            raise ApiResponseError(f"항목 필드를 해석할 수 없습니다(필드 매핑 확인 필요): {exc}") from exc
        rows.append({
            "timestamp": day.normalize() + pd.Timedelta(hours=hour),
            "region": str(it.get(f["region"], "")).strip(),
            "value": _number(it.get(f["mwh"])),
        })
    frame = pd.DataFrame(rows, columns=["timestamp", "region", "value"])
    return frame.sort_values("timestamp").reset_index(drop=True)


def fetch_day(spec: ApiSpec, day, service_key: str | None = None, timeout: float = 15.0,
              page_size: int = 500, session=None) -> pd.DataFrame:
    """하루치 응답을 가져와 표로 돌려준다. 키·엔드포인트가 없으면 호출하지 않는다."""
    key = service_key or os.environ.get(spec.key_env)
    if not key:
        raise ApiNotConfigured(f"{spec.key_env}가 설정되지 않았습니다. 공공데이터포털에서 활용신청 후 인증키를 .env에 넣어 주세요.")
    if not spec.endpoint:
        raise ApiNotConfigured(f"{spec.api_id}의 엔드포인트가 비어 있습니다. 활용신청 후 받은 명세로 config/public_apis.json을 채워 주세요.")
    p = spec.params
    day_str = pd.Timestamp(day).strftime("%Y%m%d")
    frames, page = [], 1
    http = session or requests
    while True:
        query = {p["key"]: key, p["date"]: day_str, p["page"]: page, p["rows"]: page_size}
        if p.get("format"):
            query[p["format"]] = "json"
        resp = http.get(spec.endpoint, params=query, timeout=timeout)
        resp.raise_for_status()
        items = parse_items(resp.content, spec)
        if not items:
            break
        frames.append(items_to_frame(items, spec))
        if len(items) < page_size:
            break
        page += 1
        if page > 50:
            raise ApiResponseError("페이지가 너무 많습니다(50쪽 초과) — 파라미터 확인 필요")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["timestamp", "region", "value"])
