"""오프라인 배포 검증 테스트 (ralplan S7).

토큰/네트워크 없이 실행 가능한 정적 검사:
- PycordAdapter의 privileged intent 설정 단언
- deploy/dcm.service 파일 존재 + jiwoo 잔존 식별자 없음 + ExecStart에 dcm 포함
- deploy/README.md에 cutover 핵심 키워드 포함
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# 프로젝트 루트: 이 파일의 부모 디렉터리의 부모
REPO_ROOT = Path(__file__).parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"

@pytest.fixture(autouse=True)
def _event_loop():
    """discord.Bot() 생성 시 event loop가 필요하므로 각 테스트 전에 설정."""
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    asyncio.set_event_loop(None)
    lp.close()


# ---------------------------------------------------------------------------
# 1. PycordAdapter privileged intent 설정 단언
# ---------------------------------------------------------------------------


def test_adapter_intents_message_content_and_members() -> None:
    """PycordAdapter가 message_content=True, members=True 인텐트를 설정해야 한다."""
    from dcm.platform.pycord_adapter import PycordAdapter

    adapter = PycordAdapter(
        token="test-token",
        bot_name="지우",
        guild_id=123,
    )
    intents = adapter._client.intents
    assert intents.message_content is True, "message_content intent must be True (Developer Portal 활성화 필요)"
    assert intents.members is True, "members intent must be True (on_member_join 발화 및 온보딩에 필요)"


# ---------------------------------------------------------------------------
# 2. deploy/dcm.service 파일 검사
# ---------------------------------------------------------------------------


def test_dcm_service_file_exists() -> None:
    """deploy/dcm.service 파일이 존재해야 한다."""
    service_file = DEPLOY_DIR / "dcm.service"
    assert service_file.exists(), f"deploy/dcm.service 파일이 없음: {service_file}"


def test_dcm_service_no_jiwoo_remnants() -> None:
    """deploy/dcm.service에 jiwoo 잔존 식별자가 없어야 한다."""
    service_file = DEPLOY_DIR / "dcm.service"
    content = service_file.read_text(encoding="utf-8")
    # 대소문자 구분 없이 검사(Jiwoo, jiwoo, JIWOO 등)
    assert "jiwoo" not in content.lower(), (
        "deploy/dcm.service에 jiwoo 잔존 식별자가 남아 있음:\n"
        + "\n".join(
            f"  {i+1}: {line}"
            for i, line in enumerate(content.splitlines())
            if "jiwoo" in line.lower()
        )
    )


def test_dcm_service_execstart_contains_dcm() -> None:
    """deploy/dcm.service의 ExecStart가 dcm을 실행해야 한다."""
    service_file = DEPLOY_DIR / "dcm.service"
    content = service_file.read_text(encoding="utf-8")
    exec_lines = [line for line in content.splitlines() if line.startswith("ExecStart")]
    assert exec_lines, "ExecStart 항목이 없음"
    assert any("dcm" in line for line in exec_lines), (
        f"ExecStart에 'dcm'이 없음: {exec_lines}"
    )


# ---------------------------------------------------------------------------
# 3. deploy/README.md cutover 핵심 키워드 검사
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "keyword",
    [
        "Server Members",          # privileged intent 이름
        "ADMIN_ROLE_ID",           # 필수 환경변수
        "Administrator",           # 금지 권한 언급 확인
        "DISCORD_TOKEN",           # 필수 환경변수
        "ANTHROPIC_API_KEY",       # 필수 환경변수
        "ADMIN_GUILD_ID",          # 필수 환경변수
        "Message Content",         # privileged intent 이름
        "chmod 600",               # .env 보안 지침
    ],
)
def test_readme_contains_cutover_keyword(keyword: str) -> None:
    """deploy/README.md에 cutover 필수 키워드가 포함되어야 한다."""
    readme = DEPLOY_DIR / "README.md"
    assert readme.exists(), f"deploy/README.md 파일이 없음: {readme}"
    content = readme.read_text(encoding="utf-8")
    assert keyword in content, (
        f"deploy/README.md에 cutover 키워드 '{keyword}'가 없음"
    )
