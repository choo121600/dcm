"""서버 템플릿 파서/검증 테스트 (YAML/JSON → ServerTemplate)."""
from __future__ import annotations

import json

import pytest

from dcm.service import guild_admin as ga
from dcm.service.template import (
    MAX_CATEGORIES,
    PERMISSION_BITS,
    TemplateError,
    parse_template,
)

_YAML = """
roles:
  - name: 운영진
    permissions: [manage_channels, manage_roles, kick_members]
  - name: 멤버
categories:
  - name: 2026-summer
    private: true
    visible_to: [운영진, 멤버]
    channels:
      - name: 공지
        type: text
      - name: 일반
      - name: 회의
        type: voice
  - name: 잡담
    channels:
      - name: 수다
        type: text
"""


def test_parse_yaml_full_shape():
    t = parse_template(_YAML)
    assert [r.name for r in t.roles] == ["운영진", "멤버"]
    # 권한 이름 → 비트 누적
    admin = t.roles[0]
    assert admin.permission_bits == (ga.MANAGE_CHANNELS | ga.MANAGE_ROLES | ga.KICK_MEMBERS)
    assert t.roles[1].permission_bits == 0  # permissions 생략 → 0
    # 카테고리/채널
    cat = t.categories[0]
    assert cat.name == "2026-summer" and cat.private is True
    assert cat.visible_to == ("운영진", "멤버")
    assert [(c.name, c.kind) for c in cat.channels] == [
        ("공지", "text"),
        ("일반", "text"),  # type 생략 → text 기본
        ("회의", "voice"),
    ]
    assert t.categories[1].private is False


def test_json_and_yaml_equivalent():
    """YAML은 JSON 상위호환 — 동일 내용이면 같은 결과."""
    doc = {
        "roles": [{"name": "관리", "permissions": ["ban_members"]}],
        "categories": [{"name": "C", "channels": [{"name": "ch", "type": "voice"}]}],
    }
    from_json = parse_template(json.dumps(doc, ensure_ascii=False))
    from_yaml = parse_template(
        "roles:\n  - name: 관리\n    permissions: [ban_members]\n"
        "categories:\n  - name: C\n    channels:\n      - name: ch\n        type: voice\n"
    )
    assert from_json == from_yaml
    assert from_json.roles[0].permission_bits == ga.BAN_MEMBERS


def test_unknown_permission_rejected():
    with pytest.raises(TemplateError) as e:
        parse_template("roles:\n  - name: x\n    permissions: [make_everyone_admin]\n")
    assert "알 수 없는 권한" in str(e.value)


def test_bad_channel_type_rejected():
    with pytest.raises(TemplateError) as e:
        parse_template("categories:\n  - name: C\n    channels:\n      - name: x\n        type: stage\n")
    assert "text" in str(e.value) and "voice" in str(e.value)


def test_non_mapping_top_level_rejected():
    with pytest.raises(TemplateError):
        parse_template("- 그냥\n- 리스트\n")


def test_empty_template_rejected():
    with pytest.raises(TemplateError):
        parse_template("")
    with pytest.raises(TemplateError):
        parse_template("roles: []\ncategories: []\n")  # 둘 다 비면 거부


def test_missing_name_rejected():
    with pytest.raises(TemplateError):
        parse_template("roles:\n  - permissions: [kick_members]\n")  # name 없음


def test_duplicate_names_rejected():
    with pytest.raises(TemplateError):
        parse_template("roles:\n  - name: 같음\n  - name: 같음\n")


def test_too_many_categories_rejected():
    cats = "\n".join(f"  - name: c{i}" for i in range(MAX_CATEGORIES + 1))
    with pytest.raises(TemplateError):
        parse_template("categories:\n" + cats + "\n")


def test_malformed_yaml_rejected():
    with pytest.raises(TemplateError):
        parse_template("roles: [unclosed\n")


def test_permission_aliases():
    t = parse_template("roles:\n  - name: x\n    permissions: [manage_server, timeout_members]\n")
    assert t.roles[0].permission_bits == (PERMISSION_BITS["manage_guild"] | PERMISSION_BITS["moderate_members"])


def test_summary_lists_contents():
    s = parse_template(_YAML).summary()
    assert "운영진" in s and "2026-summer" in s and "회의" in s
    assert "🔒" in s  # private 카테고리 표시
    assert "합계" in s
