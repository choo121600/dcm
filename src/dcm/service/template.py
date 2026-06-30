"""서버 블루프린트 템플릿 파서/검증 (ralplan 확장 — YAML/JSON → 셋업 계획).

LLM·discord 무관 순수 모듈. ``yaml.safe_load`` 는 JSON을 YAML의 부분집합으로 처리하므로
YAML과 JSON 템플릿을 모두 같은 경로로 파싱한다. 알 수 없는 권한 이름·채널 종류는 거부하고
(closed set + value guard), 결과는 discord-free plain-data 데이터클래스로 돌려준다.
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from .guild_admin import (
    ADMINISTRATOR,
    BAN_MEMBERS,
    KICK_MEMBERS,
    MANAGE_CHANNELS,
    MANAGE_GUILD,
    MANAGE_MESSAGES,
    MANAGE_NICKNAMES,
    MANAGE_ROLES,
    MANAGE_WEBHOOKS,
    MODERATE_MEMBERS,
)

# 템플릿에 쓰는 권한 이름 → Discord 권한 비트 (닫힌 집합; 미지의 이름은 거부).
PERMISSION_BITS: dict[str, int] = {
    "administrator": ADMINISTRATOR,
    "kick_members": KICK_MEMBERS,
    "ban_members": BAN_MEMBERS,
    "manage_channels": MANAGE_CHANNELS,
    "manage_guild": MANAGE_GUILD,
    "manage_server": MANAGE_GUILD,  # 별칭
    "manage_messages": MANAGE_MESSAGES,
    "manage_nicknames": MANAGE_NICKNAMES,
    "manage_roles": MANAGE_ROLES,
    "manage_webhooks": MANAGE_WEBHOOKS,
    "moderate_members": MODERATE_MEMBERS,
    "timeout_members": MODERATE_MEMBERS,  # 별칭
}

# 역방향(서버 → 템플릿) 추출용: 권한 비트 → 정규 이름 (별칭 제외, 표시 순서 고정).
_CANONICAL_PERMS: tuple[tuple[str, int], ...] = (
    ("administrator", ADMINISTRATOR),
    ("manage_guild", MANAGE_GUILD),
    ("manage_roles", MANAGE_ROLES),
    ("manage_channels", MANAGE_CHANNELS),
    ("manage_messages", MANAGE_MESSAGES),
    ("manage_webhooks", MANAGE_WEBHOOKS),
    ("manage_nicknames", MANAGE_NICKNAMES),
    ("kick_members", KICK_MEMBERS),
    ("ban_members", BAN_MEMBERS),
    ("moderate_members", MODERATE_MEMBERS),
)
# 템플릿이 표현할 수 있는 권한 비트 마스크 (그 외 비트는 추출 시 버림 — 스키마가 지원 안 함).
SUPPORTED_PERM_MASK = (
    ADMINISTRATOR | MANAGE_GUILD | MANAGE_ROLES | MANAGE_CHANNELS | MANAGE_MESSAGES
    | MANAGE_WEBHOOKS | MANAGE_NICKNAMES | KICK_MEMBERS | BAN_MEMBERS | MODERATE_MEMBERS
)


def bits_to_names(bits: int) -> list[str]:
    """권한 비트필드 → 템플릿 권한 이름 목록 (지원하는 관리 권한만, 고정 순서)."""
    return [name for name, bit in _CANONICAL_PERMS if bits & bit]

CHANNEL_KINDS = ("text", "voice")

MAX_NAME_LEN = 100
MAX_ROLES = 100
MAX_CATEGORIES = 50
MAX_CHANNELS_PER_CATEGORY = 50


class TemplateError(ValueError):
    """템플릿이 구조적으로 잘못됐을 때 (사용자에게 그대로 보여줄 메시지)."""


@dataclass(frozen=True)
class TemplateChannel:
    name: str
    kind: str  # "text" | "voice"


@dataclass(frozen=True)
class TemplateRole:
    name: str
    permission_bits: int
    permission_names: tuple[str, ...]


@dataclass(frozen=True)
class TemplateCategory:
    name: str
    channels: tuple[TemplateChannel, ...]
    private: bool = False
    visible_to: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServerTemplate:
    roles: tuple[TemplateRole, ...]
    categories: tuple[TemplateCategory, ...]

    def summary(self, limit: int = 1800) -> str:
        """적용 전 사람이 검토할 미리보기 텍스트 (Discord 2000자 제한 고려해 클램프)."""
        lines = ["📋 템플릿 미리보기"]
        if self.roles:
            parts = []
            for r in self.roles:
                tag = f"[{'+'.join(r.permission_names)}]" if r.permission_names else ""
                parts.append(f"{r.name}{tag}")
            lines.append("• 역할: " + ", ".join(parts))
        for c in self.categories:
            lock = " 🔒" if c.private else ""
            chs = ", ".join(
                f"{ch.name}({'🔊' if ch.kind == 'voice' else '#'})" for ch in c.channels
            )
            line = f"• 📁 {c.name}{lock}: {chs or '(채널 없음)'}"
            if c.private and c.visible_to:
                line += f"  (열람: {', '.join(c.visible_to)})"
            lines.append(line)
        total_ch = sum(len(c.channels) for c in self.categories)
        lines.append(f"합계: 역할 {len(self.roles)} · 카테고리 {len(self.categories)} · 채널 {total_ch}")
        text = "\n".join(lines)
        return text if len(text) <= limit else text[:limit] + "…(생략)"


def _req_str(value: object, ctx: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TemplateError(f"{ctx}: 비어있지 않은 문자열이어야 해.")
    s = value.strip()
    if len(s) > MAX_NAME_LEN:
        raise TemplateError(f"{ctx}: 이름이 너무 길어 (최대 {MAX_NAME_LEN}자).")
    return s


def _perm_bits(perms: object, ctx: str) -> tuple[int, tuple[str, ...]]:
    if perms is None:
        return 0, ()
    if not isinstance(perms, list):
        raise TemplateError(f"{ctx}.permissions: 목록이어야 해.")
    bits = 0
    names: list[str] = []
    for p in perms:
        if not isinstance(p, str):
            raise TemplateError(f"{ctx}.permissions: 권한 이름은 문자열이어야 해.")
        key = p.strip().lower()
        if key not in PERMISSION_BITS:
            allowed = ", ".join(sorted(set(PERMISSION_BITS)))
            raise TemplateError(f"{ctx}: 알 수 없는 권한 '{p}'. 가능: {allowed}")
        bits |= PERMISSION_BITS[key]
        names.append(key)
    return bits, tuple(names)


def parse_template(raw: str) -> ServerTemplate:
    """YAML/JSON 텍스트 → 검증된 ServerTemplate. 잘못되면 TemplateError."""
    if not isinstance(raw, str) or not raw.strip():
        raise TemplateError("빈 템플릿이야. YAML 또는 JSON 내용을 넣어줘.")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TemplateError(f"YAML/JSON 파싱 실패: {exc}") from exc
    if not isinstance(data, dict):
        raise TemplateError("최상위는 매핑(객체)이어야 해. 예: roles:, categories:")

    roles_raw = data.get("roles") or []
    cats_raw = data.get("categories") or []
    if not isinstance(roles_raw, list):
        raise TemplateError("roles는 목록이어야 해.")
    if not isinstance(cats_raw, list):
        raise TemplateError("categories는 목록이어야 해.")
    if not roles_raw and not cats_raw:
        raise TemplateError("roles 또는 categories 중 적어도 하나는 있어야 해.")
    if len(roles_raw) > MAX_ROLES:
        raise TemplateError(f"역할이 너무 많아 (최대 {MAX_ROLES}).")
    if len(cats_raw) > MAX_CATEGORIES:
        raise TemplateError(f"카테고리가 너무 많아 (최대 {MAX_CATEGORIES}).")

    roles: list[TemplateRole] = []
    seen_roles: set[str] = set()
    for i, r in enumerate(roles_raw):
        if not isinstance(r, dict):
            raise TemplateError(f"roles[{i}]: 매핑이어야 해.")
        name = _req_str(r.get("name"), f"roles[{i}].name")
        if name in seen_roles:
            raise TemplateError(f"중복 역할 이름: {name}")
        seen_roles.add(name)
        bits, names = _perm_bits(r.get("permissions"), f"roles[{i}]")
        roles.append(TemplateRole(name=name, permission_bits=bits, permission_names=names))

    cats: list[TemplateCategory] = []
    seen_cats: set[str] = set()
    for i, c in enumerate(cats_raw):
        if not isinstance(c, dict):
            raise TemplateError(f"categories[{i}]: 매핑이어야 해.")
        cname = _req_str(c.get("name"), f"categories[{i}].name")
        if cname in seen_cats:
            raise TemplateError(f"중복 카테고리 이름: {cname}")
        seen_cats.add(cname)
        private = bool(c.get("private", False))
        visible_raw = c.get("visible_to") or []
        if not isinstance(visible_raw, list):
            raise TemplateError(f"categories[{i}].visible_to: 목록이어야 해.")
        visible = tuple(_req_str(v, f"categories[{i}].visible_to[]") for v in visible_raw)
        chans_raw = c.get("channels") or []
        if not isinstance(chans_raw, list):
            raise TemplateError(f"categories[{i}].channels: 목록이어야 해.")
        if len(chans_raw) > MAX_CHANNELS_PER_CATEGORY:
            raise TemplateError(
                f"categories[{i}]: 채널이 너무 많아 (최대 {MAX_CHANNELS_PER_CATEGORY})."
            )
        chans: list[TemplateChannel] = []
        for j, ch in enumerate(chans_raw):
            if not isinstance(ch, dict):
                raise TemplateError(f"categories[{i}].channels[{j}]: 매핑이어야 해.")
            chname = _req_str(ch.get("name"), f"categories[{i}].channels[{j}].name")
            kind = ch.get("type") or "text"
            if not isinstance(kind, str) or kind.strip().lower() not in CHANNEL_KINDS:
                raise TemplateError(
                    f"categories[{i}].channels[{j}].type: 'text' 또는 'voice'여야 해 (받음: {kind!r})."
                )
            chans.append(TemplateChannel(name=chname, kind=kind.strip().lower()))
        cats.append(
            TemplateCategory(
                name=cname, channels=tuple(chans), private=private, visible_to=visible
            )
        )

    return ServerTemplate(roles=tuple(roles), categories=tuple(cats))


def to_yaml(template: ServerTemplate) -> str:
    """ServerTemplate → YAML 문자열. parse_template로 다시 읽을 수 있는 형식(round-trip)."""
    data: dict = {}
    if template.roles:
        roles_out = []
        for r in template.roles:
            entry: dict = {"name": r.name}
            if r.permission_names:
                entry["permissions"] = list(r.permission_names)
            roles_out.append(entry)
        data["roles"] = roles_out
    if template.categories:
        cats_out = []
        for c in template.categories:
            entry = {"name": c.name}
            if c.private:
                entry["private"] = True
            if c.visible_to:
                entry["visible_to"] = list(c.visible_to)
            entry["channels"] = [{"name": ch.name, "type": ch.kind} for ch in c.channels]
            cats_out.append(entry)
        data["categories"] = cats_out
    if not data:
        data = {"categories": []}
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
