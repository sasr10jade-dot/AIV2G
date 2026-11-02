"""LLM 게이트웨이 — EV Bot이 외부 LLM(Gemini)을 부를 때 지나가는 단일 관문.

무료 쿼터로 운영해도 서비스가 멈추거나 이상한 답이 나가지 않도록 다음을 한곳에서 처리한다.
  1. 개인정보 마스킹: 번호판·전화번호·이메일은 가리고 차량 ID는 가명으로 바꿔 보낸다(응답에서 복원).
  2. 호출 한도: 분당 토큰버킷 + 일일 상한. 넘으면 호출하지 않고 템플릿 답변으로 대체한다.
  3. 캐시: 같은 입력은 TTL 안에서 재사용해 쿼터를 아낀다.
  4. 재시도: 일시 오류(429/5xx)만 지수 백오프로 재시도한다.
  5. 숫자 검증: 답변의 금액·kWh·kW·% 숫자가 근거(facts)에 없으면 버리고 템플릿으로 대체한다.
  6. 템플릿 대체: 위 어느 단계에서 막혀도 근거 데이터로 만든 결정적 문장을 돌려준다.

프로필(LLM_PROFILE 환경변수): demo(기본, 무료 쿼터 보수적 한도) | pilot(유료 계정 전제, 한도 상향) |
local(외부 호출 없음 — 템플릿만). 무료 티어는 입력이 제공사 개선에 쓰일 수 있으므로 어떤 프로필에서도
식별정보는 마스킹해서만 내보낸다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from src.privacy.pseudonym import pseudo_id


class TransientLlmError(Exception):
    """재시도할 만한 일시 오류(429, 5xx, 네트워크)."""


@dataclass(frozen=True)
class Profile:
    name: str
    use_llm: bool
    per_minute: int
    daily_cap: int
    cache_ttl_s: float
    max_retries: int


PROFILES = {
    "demo": Profile("demo", True, per_minute=8, daily_cap=200, cache_ttl_s=300, max_retries=2),
    "pilot": Profile("pilot", True, per_minute=30, daily_cap=5000, cache_ttl_s=120, max_retries=2),
    "local": Profile("local", False, per_minute=0, daily_cap=0, cache_ttl_s=0, max_retries=0),
}


def active_profile() -> Profile:
    return PROFILES.get(os.environ.get("LLM_PROFILE", "demo"), PROFILES["demo"])


# ----------------------------------------------------------------------------- 마스킹

_PLATE = re.compile(r"(?:[가-힣]{2})?\d{2,3}[가-힣]\d{4}")
_PHONE = re.compile(r"01\d[- ]?\d{3,4}[- ]?\d{4}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_EVID = re.compile(r"\bEV-[A-Za-z0-9-]+\b")


def mask_text(text: str) -> tuple[str, dict[str, str]]:
    """(마스킹된 텍스트, {가명: 원래 ev_id}) — 가명은 응답에서 되돌리는 데 쓴다."""
    restore: dict[str, str] = {}

    def ev_sub(m: re.Match) -> str:
        p = pseudo_id("ev", m.group(0))
        restore[p] = m.group(0)
        return p

    out = _PLATE.sub("[번호판]", text)
    out = _PHONE.sub("[전화번호]", out)
    out = _EMAIL.sub("[이메일]", out)
    out = _EVID.sub(ev_sub, out)
    return out, restore


def unmask_text(text: str, restore: dict[str, str]) -> str:
    for pseudo, real in restore.items():
        text = text.replace(pseudo, real)
    return text


# ----------------------------------------------------------------------------- 숫자 검증

_UNIT_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(원|kWh|kW|%)")


def _flatten_numbers(obj, acc: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, str):  # 문서·JSON 문자열 안의 숫자도 근거로 인정
        acc.update(float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", obj) if x.replace(",", "").replace(".", "", 1).isdigit())
    elif isinstance(obj, (int, float)):
        acc.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten_numbers(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flatten_numbers(v, acc)


def unsupported_numbers(answer: str, facts, extra_allowed: tuple[float, ...] = ()) -> list[str]:
    """답변의 '숫자+단위(원/kWh/kW/%)' 중 근거에 없는 것. 시각(HH시)·건수는 검사하지 않는다."""
    allowed: set[float] = set(extra_allowed)
    _flatten_numbers(facts, allowed)
    bad = []
    for m in _UNIT_NUM.finditer(answer):
        value = float(m.group(1).replace(",", ""))
        if not any(abs(value - a) <= max(0.51, 0.005 * abs(a)) for a in allowed):
            bad.append(m.group(0))
    return bad


# ----------------------------------------------------------------------------- 게이트웨이

@dataclass
class GatewayResult:
    text: str
    source: str   # llm | cache | template
    reason: str | None = None  # template일 때 이유: local_profile | quota | error | unsupported_numbers


class LlmGateway:
    def __init__(self, call_fn: Callable[[str, str], str], profile: Profile | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.call_fn = call_fn
        self.profile = profile or active_profile()
        self.clock, self.sleep = clock, sleep
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._tokens = float(self.profile.per_minute)
        self._refilled = clock()
        self._day, self._day_count = None, 0

    # --- 한도
    def _allow_call(self) -> bool:
        p = self.profile
        now = self.clock()
        self._tokens = min(p.per_minute, self._tokens + (now - self._refilled) * p.per_minute / 60.0)
        self._refilled = now
        today = time.strftime("%Y-%m-%d")
        if self._day != today:
            self._day, self._day_count = today, 0
        if self._tokens < 1 or self._day_count >= p.daily_cap:
            return False
        self._tokens -= 1
        self._day_count += 1
        return True

    # --- 캐시
    def _cache_get(self, key: str) -> str | None:
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] <= self.profile.cache_ttl_s:
            self._cache.move_to_end(key)
            return hit[1]
        self._cache.pop(key, None)
        return None

    def _cache_put(self, key: str, value: str) -> None:
        self._cache[key] = (self.clock(), value)
        while len(self._cache) > 256:
            self._cache.popitem(last=False)

    # --- 호출
    def generate(self, system: str, prompt: str, facts, template: Callable[[], str],
                 extra_allowed: tuple[float, ...] = ()) -> GatewayResult:
        if not self.profile.use_llm:
            return GatewayResult(template(), "template", "local_profile")

        masked_prompt, restore = mask_text(prompt)
        key = hashlib.sha256((system + "\x00" + masked_prompt).encode("utf-8")).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            return GatewayResult(unmask_text(cached, restore), "cache")

        if not self._allow_call():
            return GatewayResult(template(), "template", "quota")

        attempt = 0
        while True:
            try:
                raw = self.call_fn(system, masked_prompt)
                break
            except TransientLlmError:
                if attempt >= self.profile.max_retries:
                    return GatewayResult(template(), "template", "error")
                self.sleep(min(2 ** attempt, 8))
                attempt += 1
            except Exception:
                return GatewayResult(template(), "template", "error")

        text = unmask_text((raw or "").strip(), restore)
        if not text or unsupported_numbers(text, facts, extra_allowed):
            return GatewayResult(template(), "template", "unsupported_numbers" if text else "error")
        self._cache_put(key, raw.strip())
        return GatewayResult(text, "llm")


# ----------------------------------------------------------------------------- 템플릿 답변

def _won(x) -> str:
    return f"{int(round(float(x))):,}원"


def template_next_session(plan: dict) -> str:
    """plan_next_session 결과를 결정적 문장으로. LLM 없이도 같은 근거로 같은 안내를 한다."""
    parts = []
    dep = str(plan.get("departure_time", ""))[11:13]
    if dep:
        parts.append(f"{dep}시 출발 기준 계획입니다.")
    ch = plan.get("charge_intervals") or []
    dc = plan.get("discharge_intervals") or []
    if ch:
        parts.append(f"{ch[0]['time'][11:13]}시~{ch[-1]['time'][11:13]}시 사이 충전({plan.get('total_charge_kwh')}kWh)이 유리합니다.")
    if dc:
        parts.append(f"{dc[0]['time'][11:13]}시~{dc[-1]['time'][11:13]}시 사이 방전({plan.get('total_discharge_kwh')}kWh)을 권합니다.")
    else:
        parts.append("이번에는 방전 이득이 크지 않아 충전만 권합니다.")
    parts.append(f"예상 순이익은 {_won(plan.get('net_krw', 0))}입니다.")
    if (plan.get("assumptions") or {}).get("verified") is False:
        parts.append("(가정값 기준 예상)")
    return " ".join(parts)


def template_session(plan: dict) -> str:
    return template_next_session(plan)
