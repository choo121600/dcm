"""홍보/공지 문구 다듬기 (LLM 사용, discord-free).

운영진이 입력한 공지/행사 문구의 **톤만** 다듬는다. 날짜·시간·장소·링크 등 사실은
절대 바꾸거나 지어내지 않으며, 실패 시 원문을 그대로 돌려주는 fail-open 정책이다.
행사 공지의 날짜/카운트다운 라벨은 이 문구와 별개로 코드가 결정하므로, 다듬기가
사실 정확성을 훼손하지 않는다.
"""
from __future__ import annotations

MAX_POLISHED_CHARS = 600  # 공지 한 덩어리로 과하지 않게 상한


def _system(bot_name: str, kind: str) -> str:
    what = "행사/이벤트 홍보 공지" if kind == "event" else "공지"
    return (
        f"너는 '{bot_name}', 디스코드 커뮤니티 관리자야. 운영진이 준 {what} 문구를 "
        "더 매력적이고 읽기 좋게 '다듬기'만 해.\n"
        "규칙:\n"
        "- 날짜·시간·장소·링크·인원·가격 같은 사실은 절대 바꾸거나 지어내지 마.\n"
        "- 원문에 없는 정보를 새로 추가하지 마.\n"
        "- 한국어, 커뮤니티 톤으로 자연스럽게. 이모지는 0~3개까지만.\n"
        "- 2~4문장 이내로 간결하게.\n"
        "- 다듬은 결과 문구'만' 출력해. 설명·따옴표·머리말 없이."
    )


async def polish_copy(
    llm,
    *,
    bot_name: str,
    raw: str,
    kind: str = "event",
    title: str | None = None,
) -> str:
    """운영진 문구 `raw` 를 다듬어 반환. 빈 입력/LLM 실패/빈 응답이면 원문 그대로."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    header = f"[행사명] {title.strip()}\n" if title and title.strip() else ""
    user = f"{header}[원문]\n{raw}\n\n위 문구를 다듬어줘."
    try:
        text, _ = await llm.complete(
            system=_system(bot_name, kind),
            messages=[{"role": "user", "content": user}],
        )
    except Exception:  # noqa: BLE001 - LLM 실패는 원문 유지(공지 자체는 막지 않음)
        return raw
    text = (text or "").strip().strip('"').strip()
    return text[:MAX_POLISHED_CHARS] if text else raw
