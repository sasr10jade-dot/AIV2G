"""AI Gemini (Gemini 기반) — 프로젝트 문서 + 실시간 대시보드 데이터를 근거로 질의응답한다.

RAG 방식: 문서 코퍼스가 작아(수십 KB) 별도 벡터 검색 없이, 관련 문서 전문 + 현재
시나리오 데이터를 통째로 프롬프트에 넣는 "stuff" 방식을 쓴다. 대시보드 스냅샷은
프런트엔드가 마지막으로 조회한 값을 그대로 넘겨줘 서버가 매 질문마다 무거운
파이프라인을 다시 계산하지 않게 한다.
"""
import json
import logging
import os

from google import genai
from google.genai import types

from src.copilot.gateway import (
    LlmGateway, TransientLlmError, active_profile, template_next_session, template_session,
)
from src.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

DOC_FILES = [
    # 사업수행계획서 원문 발췌 — scripts/build_evbot_docs.py 로 계획서(pptx)에서 생성
    "docs/evbot/사업수행계획서_발췌_v1.0.md",
    "docs/evbot/모바일웹_서비스_안내.md",
    "docs/dev_progress_memo_20261002.md",
]

# 토큰·쿼터 절약을 위해 가장 가벼운 flash-lite 계열을 기본으로 한다. 품질이 모자라면 .env의
# GEMINI_MODEL로 교체(예: gemini-3.5-flash). 호출 시점에 읽으므로 재시작 없이 바꿀 수 있는 곳은 없지만
# 코드 수정 없이 설정만으로 바뀐다.
DEFAULT_MODEL = "gemini-3.5-flash-lite"


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL


MODEL_NAME = DEFAULT_MODEL  # 하위 호환(참조용)

SYSTEM_INSTRUCTION = (
    "당신은 'AI기반 도시형 에너지 이익공유 및 전기차 V2G기술 실증' 프로젝트의 AI Gemini입니다. "
    "아래 제공된 사업계획 문서와 현재 대시보드 시나리오 데이터를 근거로만 답변하세요. "
    "'질문자 차량의 실제 LP 최적화 충전/방전 계획'(이미 지난/관측된 세션)이 제공되면 그 차량에 "
    "대한 과거·현재 질문에 그 JSON의 실제 수치만 사용해 답하세요. "
    "'질문자 차량의 예측된 다음 세션 계획'(아직 일어나지 않은 미래 예측)이 제공되면, '내일 언제 "
    "나가서 언제 충전/방전하면 얼마 벌어?' 같은 미래 질문에 그 JSON만 근거로 답하되, 반드시 "
    "'~할 것으로 예상됩니다', '~하면 됩니다' 같은 예측 어투를 쓰고 이미 확정된 사실처럼 말하지 마세요. "
    "'질문자 차량의 지금 이 순간 상태'(OBD 스냅샷)가 제공되면, 'SOC 몇이야', '지금 충전중이야' 같은 "
    "실시간 상태 질문에 그 값만 근거로 답하세요 — 시계열 전체가 아니라 가장 최근 한 시점의 스냅샷임을 "
    "인지하고 답하세요. "
    "'이전 대화'가 제공되면 그 맥락을 참고해 후속 질문("
    "예: '그럼 얼마야?')에 자연스럽게 답하되, 이전 대화에서 나온 수치를 새로 지어내지 말고 다시 "
    "필요하면 제공된 데이터에서 그대로 가져오세요. "
    "'통계 요약본'이 제공되면 그건 과거 여러 번의 시나리오 실행 이력을 집계한 추세 정보입니다 — "
    "'지난번보다 나아졌어?', '요즘 추세가 어때?' 같은 질문에 그 수치만 근거로 답하세요. 요약본이 "
    "\"이력이 부족하다\"고 명시하면 그 사실을 그대로 전달하고 추세를 지어내지 마세요. "
    "위 데이터 모두 수치를 지어내지 마세요 — 문서나 데이터에 없는 내용은 추측하지 말고 모른다고 답하세요. "
    "한국어로, 간결하고 정확하게 답변하세요."
)


_GATEWAY: LlmGateway | None = None
_TRANSIENT_CODES = (429, 500, 502, 503, 504)


def _gemini_call(system: str, prompt: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    # 응답이 없을 때 요청 스레드가 무한정 붙잡히지 않도록 호출 시간 제한을 둔다(초과하면 일시 오류로 취급해 재시도·템플릿 대체)
    timeout_ms = int(float(os.environ.get("GEMINI_TIMEOUT_S", "25")) * 1000)
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
    try:
        response = client.models.generate_content(model=model_name(), contents=prompt, config={"system_instruction": system})
    except Exception as exc:
        text = str(exc)
        if (getattr(exc, "code", None) in _TRANSIENT_CODES or "RESOURCE_EXHAUSTED" in text or "UNAVAILABLE" in text
                or "timed out" in text.lower() or "timeout" in text.lower()):
            raise TransientLlmError(text[:200]) from exc
        raise
    return response.text


def _plan_extra_numbers(plan: dict) -> tuple[float, ...]:
    """계획 JSON에 직접 없지만 정당한 파생 수치(충전·방전 시간 구간의 kWh 합 등)."""
    extra = []
    for key in ("charge_intervals", "discharge_intervals"):
        extra.append(sum(float(i.get("kw", 0)) for i in plan.get(key, [])))
    return tuple(extra)


def get_gateway() -> LlmGateway:
    """프로세스당 하나 — 캐시·호출 한도가 요청 사이에 유지돼야 쿼터 보호가 의미 있다."""
    global _GATEWAY
    if _GATEWAY is None or _GATEWAY.profile.name != active_profile().name:
        _GATEWAY = LlmGateway(_gemini_call)
    return _GATEWAY


def _require_key_unless_local() -> None:
    if active_profile().use_llm and not os.environ.get("GEMINI_API_KEY"):
        raise CopilotNotConfigured(
            "GEMINI_API_KEY가 설정되지 않았습니다. 프로젝트 루트의 .env 파일에 키를 넣어주세요."
        )


class CopilotNotConfigured(Exception):
    """GEMINI_API_KEY가 설정되지 않은 경우."""


def _load_docs() -> str:
    parts = []
    for filename in DOC_FILES:
        path = PROJECT_ROOT / filename
        if path.exists():
            parts.append(f"### 문서: {filename}\n{path.read_text(encoding='utf-8')}")
        else:
            # 문서가 없으면 EV Bot이 "문서에 내용이 없다"고만 답하게 된다(과거에 조용히 실패했음) — 반드시 알린다
            logger.warning("EV Bot 참고 문서를 찾을 수 없습니다: %s", filename)
    return "\n\n".join(parts)


MAX_HISTORY_TURNS = 2  # 최근 N개 질문/답변 쌍만 프롬프트에 포함(길이 제어)


def ask(
    question: str,
    dashboard_snapshot: dict | None = None,
    vehicle_plan: dict | None = None,
    next_session_plan: dict | None = None,
    vehicle_status: dict | None = None,
    history: list[dict] | None = None,
    stat_summary: str | None = None,
) -> str:
    _require_key_unless_local()

    context = _load_docs()
    if dashboard_snapshot:
        context += "\n\n### 현재 대시보드 시나리오 데이터 (JSON)\n" + json.dumps(
            dashboard_snapshot, ensure_ascii=False
        )
    if vehicle_status:
        # build_vehicle_status_table의 스냅샷 한 행 — "지금 SOC 몇이야?" 같은
        # 실시간 상태 질문에 답하기 위한 근거(시계열 전체가 아니라 최신 한 시점).
        context += "\n\n### 질문자 차량의 지금 이 순간 상태 (JSON, OBD 스냅샷) — 이 값만 근거로 답하세요\n" + json.dumps(
            vehicle_status, ensure_ascii=False
        )
    if vehicle_plan:
        # explain-schedule과 같은 실제 LP 최적화 결과(이미 지난 세션) — "내 차 계획"
        # 과거·현재 질문에 이 값만 근거로 답하도록 SYSTEM_INSTRUCTION에서 지시한다.
        context += "\n\n### 질문자 차량의 실제 LP 최적화 충전/방전 계획 (JSON, 이미 지난 세션) — 이 값만 근거로 답하세요\n" + json.dumps(
            vehicle_plan, ensure_ascii=False
        )
    if next_session_plan:
        # plan_next_session과 같은 예측 기반 결과(아직 일어나지 않은 미래) — "내일
        # 얼마 벌어?" 같은 미래 질문에 이 값을 근거로 답하되 예측 어투를 쓰도록 지시한다.
        context += "\n\n### 질문자 차량의 예측된 다음 세션 계획 (JSON, 아직 일어나지 않은 예측) — 이 값만 근거로 답하세요\n" + json.dumps(
            next_session_plan, ensure_ascii=False
        )
    if stat_summary:
        # 과거 여러 번의 시나리오 실행 이력을 집계한 추세 텍스트 — "지난번보다
        # 나아졌어?" 같은 질문에 대한 근거(dashboard_run_history 기반, dashboard.py가
        # 실제 새 계산을 할 때마다 누적).
        context += "\n\n### 통계 요약본 (과거 실행 이력 기반 추세) — 이 값만 근거로 답하세요\n" + stat_summary

    history_text = ""
    if history:
        # 최근 몇 턴만 남겨 "그럼 얼마야?" 같은 후속 질문이 맥락을 이어가게 한다 —
        # 대화 전체를 무한정 쌓으면 프롬프트가 계속 길어지므로 최근 것만 유지.
        recent = history[-MAX_HISTORY_TURNS * 2 :]
        lines = [f"{'사용자' if h.get('role') == 'user' else 'AI'}: {h.get('text', '')}" for h in recent]
        history_text = "\n\n### 이전 대화 (참고용, 여기 나온 수치를 새로 지어내지 말 것)\n" + "\n".join(lines)

    prompt = f"{context}{history_text}\n\n---\n\n질문: {question}"

    if not active_profile().use_llm:
        raise CopilotNotConfigured("local 프로필에서는 자유 질의를 지원하지 않습니다(LLM_PROFILE=demo/pilot 필요).")
    result = get_gateway().generate(
        SYSTEM_INSTRUCTION, prompt, facts=context + history_text,
        template=lambda: "지금은 AI 답변을 드리기 어렵습니다(요청이 많거나 일시 오류). 잠시 후 다시 시도해 주세요.",
    )
    return result.text


SCHEDULE_SYSTEM_INSTRUCTION = (
    "당신은 EV 차주에게 오늘의 최적 충전·V2G 방전 계획을 설명해주는 AI 비서입니다. "
    "아래 JSON은 실제 LP(선형계획) 최적화 엔진이 계산한 결과입니다 — 숫자를 지어내지 말고 "
    "JSON에 있는 값만 사용해서 설명하세요. 사용자가 바로 이해할 수 있게 "
    "'몇 시부터 몇 시까지 충전/방전하면 얼마를 번다'는 형태로, 한국어로 2~3문장 이내로 "
    "간결하게 답하세요. 시각은 'HH시' 형식으로, 금액은 '원' 단위 천단위 콤마로 표시하세요."
)


NEXT_SESSION_SYSTEM_INSTRUCTION = (
    "당신은 EV 차주에게 내일(다음 세션)의 예상 충전/V2G 방전 계획을 미리 안내하는 AI 비서입니다. "
    "아래 JSON은 이력 기반 출차 시각 예측 + 그 구간에 대한 실제 LP 최적화 결과입니다 — 숫자를 "
    "지어내지 말고 JSON에 있는 값만 사용하세요. 이건 이미 일어난 일이 아니라 예측/추천이므로, "
    "'~할 것으로 예상됩니다', '~하면 됩니다' 같은 예측·추천 어투를 쓰고 이미 확정된 사실처럼 "
    "말하지 마세요. '몇 시에 출차하실 것으로 예상되니, 몇 시부터 몇 시까지 충전/방전하면 얼마를 "
    "벌 수 있다'는 형태로 한국어 2~3문장 이내로 간결하게 답하세요. 시각은 'HH시', 금액은 '원' "
    "단위 천단위 콤마로 표시하세요."
)


def explain_next_session_plan(plan: dict) -> str:
    """`plan_next_session`이 계산한 예측 기반 다음 세션 계획을 자연어 안내문으로
    바꾼다 — 이미 끝난 세션을 설명하는 `explain_v2g_schedule`과 달리 아직 일어나지
    않은 예측이므로 시스템 지침에서 어투를 예측형으로 구분한다."""
    _require_key_unless_local()

    prompt = (
        "다음은 한 EV의 예측된 다음 세션(출차 예정 시각까지) 충전/V2G 방전 계획입니다(JSON):\n"
        + json.dumps(plan, ensure_ascii=False)
        + "\n\n이 계획을 차주에게 미리 안내하는 문장을 작성하세요. 금액은 JSON의 net_krw(열화비용 차감 후 순이익)를 "
        "기준으로 말하고, assumptions.verified가 false이면 '가정값 기준 예상'임을 한 번 밝히세요. 없는 값은 지어내지 마세요."
    )
    return get_gateway().generate(
        NEXT_SESSION_SYSTEM_INSTRUCTION, prompt, facts=plan, template=lambda: template_next_session(plan),
        extra_allowed=_plan_extra_numbers(plan),
    ).text


def explain_v2g_schedule(plan: dict) -> str:
    """optimizer가 계산한 세션 스케줄(`poc_compare.summarize_session_plan`)을 사람이 읽을
    자연어 설명으로 바꾼다 — plan의 모든 숫자는 실제 LP 최적화 결과이며 이 함수는 그것을
    문장으로 옮기기만 한다(수치를 새로 만들지 않음)."""
    _require_key_unless_local()

    prompt = (
        "다음은 한 EV 세션의 최적 충전/V2G 방전 계획입니다(JSON):\n"
        + json.dumps(plan, ensure_ascii=False)
        + "\n\n이 계획을 차주에게 설명하는 문장을 작성하세요."
    )
    return get_gateway().generate(
        SCHEDULE_SYSTEM_INSTRUCTION, prompt, facts=plan, template=lambda: template_session(plan),
        extra_allowed=_plan_extra_numbers(plan),
    ).text
