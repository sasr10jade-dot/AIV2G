"""추천 근거 설명 LLM 생성 — 요구사항 D05(계획서 역할표 'V2G 의사결정 모델(LLM 설명)').

최적화(soc_planner)가 정한 충전·방전 계획의 수치만 Claude에 넘겨, 차주가 읽기 쉬운 근거 설명을 만든다.

원칙
- 개인정보를 보내지 않는다: 차량 ID·계정·번호판·거주지·직업군은 넣지 않고, 시간 구간·kWh·원·배터리 %만 보낸다.
- 숫자를 지어내지 않게 한다: 넘긴 수치만 쓰고, 모의·추론 값임을 밝히도록 지시한다.
- 실패해도 안내는 계속된다: 키가 없거나(ANTHROPIC_API_KEY), 꺼져 있거나(LLM_EXPLANATION=0), 시간 초과·오류·거절이면
  None을 돌려주고 호출 측이 기존 문장 틀(rule_template) 설명을 쓴다.
- 같은 계획 수치·언어는 1시간 동안 캐시해 같은 안내를 다시 열 때 다시 호출하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
CACHE_TTL_SEC = 3600
CACHE_MAX = 500
_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()

SYSTEM_KO = (
    "당신은 전기차 충전 안내 앱에서 'AI가 왜 이 충전 시간을 추천했는지'를 차주에게 설명하는 역할입니다.\n"
    "규칙:\n"
    "- 사용자 메시지의 JSON에 있는 수치와 사실만 사용하세요. 새로운 숫자·요금·효과를 만들지 마세요.\n"
    "- 3~4문장, 존댓말(해요체), 쉬운 우리말로 씁니다. 목록·마크다운·이모지 없이 한 문단으로만 답하세요.\n"
    "- 무엇을(추천 충전 구간), 왜(싼 요금·계통 부하·출발 목표), 얼마나(절감액·방전 편익)를 순서대로 설명합니다.\n"
    "- 절감액·방전 편익은 실제 정산이 아닌 모의·추론 값이라는 점을 한 번 밝힙니다.\n"
    "- 방전을 추천하지 않으면 그 이유(v2g_reason)를 자연스럽게 녹여 씁니다."
)
SYSTEM_EN = SYSTEM_KO + "\n- Answer in English instead of Korean, same rules."


def enabled() -> bool:
    if os.environ.get("LLM_EXPLANATION", "1") == "0":
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _key(facts: dict, lang: str) -> str:
    return hashlib.sha256((lang + json.dumps(facts, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()


def _call(facts: dict, lang: str) -> str | None:
    import anthropic

    client = anthropic.Anthropic(timeout=20.0, max_retries=1)
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                       # 안전 분류로 거절되면 서버가 다른 모델로 이어서 답한다
            output_config={"effort": "low"},          # 짧은 설명문 — 낮은 effort로 충분
            system=SYSTEM_EN if lang == "en" else SYSTEM_KO,
            messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
        )
    except anthropic.RateLimitError:
        logger.warning("LLM 설명: 호출 한도 초과 — 문장 틀로 대체")
        return None
    except anthropic.APIStatusError as exc:
        logger.warning("LLM 설명: API 오류 %s — 문장 틀로 대체", exc.status_code)
        return None
    except anthropic.APIConnectionError:
        logger.warning("LLM 설명: 연결 실패 — 문장 틀로 대체")
        return None
    if response.stop_reason == "refusal":
        return None
    text = " ".join(b.text for b in response.content if b.type == "text").strip()
    return text or None


def explain(facts: dict, lang: str = "ko", call=None) -> str | None:
    """계획 수치(facts)로 근거 설명을 만든다. 쓸 수 없으면 None. call은 테스트용 주입."""
    if call is None and not enabled():
        return None
    k = _key(facts, lang)
    with _lock:
        hit = _cache.get(k)
        if hit and time.time() - hit[0] < CACHE_TTL_SEC:
            return hit[1]
    try:
        text = (call or _call)(facts, lang)
    except Exception:   # 설명 실패가 충전 안내 자체를 막으면 안 된다
        logger.exception("LLM 설명 생성 실패 — 문장 틀로 대체")
        return None
    if text:
        with _lock:
            if len(_cache) >= CACHE_MAX:
                _cache.clear()
            _cache[k] = (time.time(), text)
    return text
