"""서포터즈 충전 인증 수단(회원카드·RFID 태그, OCPP idTag) ↔ 차량 매핑 — 평가의견 4·14 보완(P3).

충전사업자 세션에는 차량 ID가 없고 인증 태그만 온다. 서포터즈가 가입 때 쓰는 충전 카드를 등록해 두면
수신 세션의 태그로 차량을 찾아 차량 OBD 충전 이벤트와 충전기 세션을 결합(A 서포터즈 결합형)할 수 있다.

태그 원문은 저장하지 않는다 — HMAC 가명(PT-…)만 data/charge_tags.json에 둔다(같은 키로 수신 태그를 가명화해 비교).
"""
from __future__ import annotations

import json
from datetime import datetime

from src.paths import DATA_DIR
from src.privacy.pseudonym import pseudo_id

TAGS_FILE = DATA_DIR / "charge_tags.json"


def _load() -> dict:
    try:
        return json.loads(TAGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TAGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TAGS_FILE)


def _norm(id_tag: str) -> str:
    return str(id_tag or "").strip().upper()


def register(ev_id: str, id_tag: str, by: str | None = None) -> dict:
    tag = _norm(id_tag)
    if not ev_id or not tag:
        raise ValueError("차량 ID와 인증 태그가 모두 필요합니다")
    data = _load()
    key = pseudo_id("tag", tag)
    data[key] = {"ev_id": str(ev_id), "registered_at": datetime.now().isoformat(timespec="seconds"), "by": by}
    _save(data)
    return {"tag_pseudo_id": key, "ev_id": str(ev_id)}


def unregister(tag_pseudo_id: str) -> bool:
    data = _load()
    if tag_pseudo_id not in data:
        return False
    data.pop(tag_pseudo_id)
    _save(data)
    return True


def list_tags() -> list[dict]:
    return [{"tag_pseudo_id": k, **v} for k, v in sorted(_load().items(), key=lambda kv: kv[1].get("ev_id", ""))]


def resolve(id_tag) -> str | None:
    tag = _norm(id_tag)
    if not tag or tag in ("NAN", "NONE"):
        return None
    hit = _load().get(pseudo_id("tag", tag))
    return hit["ev_id"] if hit else None


def fill_ev_ids(df):
    """세션 표의 id_tag로 비어 있는 ev_id를 채우고 id_tag 열은 지운다(원문 미보관). 채운 건수를 함께 돌려준다."""
    if df is None or "id_tag" not in df.columns:
        return df, 0
    out = df.copy()
    if "ev_id" not in out.columns:
        out["ev_id"] = None
    empty = out["ev_id"].isna() | (out["ev_id"].astype(str).str.strip().isin(["", "None", "nan"]))
    mapping = _load()
    filled = 0
    if mapping:
        def _one(v):
            t = _norm(v)
            hit = mapping.get(pseudo_id("tag", t)) if t and t not in ("NAN", "NONE") else None
            return hit["ev_id"] if hit else None
        resolved = out.loc[empty, "id_tag"].map(_one)
        out.loc[resolved.index, "ev_id"] = resolved
        filled = int(resolved.notna().sum())
    return out.drop(columns=["id_tag"]), filled
