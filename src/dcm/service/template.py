"""Server-blueprint template parser/validator (ralplan extension — YAML/JSON → setup plan).

LLM- and discord-agnostic pure module. Since ``yaml.safe_load`` treats JSON as a subset of YAML,
both YAML and JSON templates are parsed through the same path. Unknown permission names and channel
kinds are rejected (closed set + value guard), and the result is returned as discord-free plain-data
dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from ..i18n import t
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

# Permission names used in templates → Discord permission bits (closed set; unknown names rejected).
PERMISSION_BITS: dict[str, int] = {
    "administrator": ADMINISTRATOR,
    "kick_members": KICK_MEMBERS,
    "ban_members": BAN_MEMBERS,
    "manage_channels": MANAGE_CHANNELS,
    "manage_guild": MANAGE_GUILD,
    "manage_server": MANAGE_GUILD,  # alias
    "manage_messages": MANAGE_MESSAGES,
    "manage_nicknames": MANAGE_NICKNAMES,
    "manage_roles": MANAGE_ROLES,
    "manage_webhooks": MANAGE_WEBHOOKS,
    "moderate_members": MODERATE_MEMBERS,
    "timeout_members": MODERATE_MEMBERS,  # alias
}

# For reverse (server → template) extraction: permission bit → canonical name (aliases excluded, fixed display order).
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
# Bitmask of permissions the template can express (other bits are dropped on extraction — schema doesn't support them).
SUPPORTED_PERM_MASK = (
    ADMINISTRATOR | MANAGE_GUILD | MANAGE_ROLES | MANAGE_CHANNELS | MANAGE_MESSAGES
    | MANAGE_WEBHOOKS | MANAGE_NICKNAMES | KICK_MEMBERS | BAN_MEMBERS | MODERATE_MEMBERS
)


def bits_to_names(bits: int) -> list[str]:
    """Permission bitfield → list of template permission names (supported management perms only, fixed order)."""
    return [name for name, bit in _CANONICAL_PERMS if bits & bit]

CHANNEL_KINDS = ("text", "voice")

MAX_NAME_LEN = 100
MAX_ROLES = 100
MAX_CATEGORIES = 50
MAX_CHANNELS_PER_CATEGORY = 50


class TemplateError(ValueError):
    """Raised when a template is structurally invalid (message shown to the user as-is)."""


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
        """Preview text for a human to review before applying (clamped for Discord's 2000-char limit)."""
        lines = [t("template.preview_title")]
        if self.roles:
            parts = []
            for r in self.roles:
                tag = f"[{'+'.join(r.permission_names)}]" if r.permission_names else ""
                parts.append(f"{r.name}{tag}")
            lines.append(t("template.role_line", roles=", ".join(parts)))
        for c in self.categories:
            lock = " 🔒" if c.private else ""
            chs = ", ".join(
                f"{ch.name}({'🔊' if ch.kind == 'voice' else '#'})" for ch in c.channels
            )
            chs_display = chs or t("template.no_channels")
            line = t("template.category_line", name=c.name, lock=lock, chs=chs_display)
            if c.private and c.visible_to:
                line += t("template.visible_suffix", names=", ".join(c.visible_to))
            lines.append(line)
        total_ch = sum(len(c.channels) for c in self.categories)
        lines.append(
            t(
                "template.totals",
                roles=len(self.roles),
                categories=len(self.categories),
                channels=total_ch,
            )
        )
        text = "\n".join(lines)
        return text if len(text) <= limit else text[:limit] + t("template.truncated_suffix")


def _req_str(value: object, ctx: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TemplateError(t("template.err_req_str", ctx=ctx))
    s = value.strip()
    if len(s) > MAX_NAME_LEN:
        raise TemplateError(t("template.err_name_too_long", ctx=ctx, max=MAX_NAME_LEN))
    return s


def _perm_bits(perms: object, ctx: str) -> tuple[int, tuple[str, ...]]:
    if perms is None:
        return 0, ()
    if not isinstance(perms, list):
        raise TemplateError(t("template.err_perms_list", ctx=ctx))
    bits = 0
    names: list[str] = []
    for p in perms:
        if not isinstance(p, str):
            raise TemplateError(t("template.err_perm_name_str", ctx=ctx))
        key = p.strip().lower()
        if key not in PERMISSION_BITS:
            allowed = ", ".join(sorted(set(PERMISSION_BITS)))
            raise TemplateError(t("template.err_unknown_perm", ctx=ctx, p=p, allowed=allowed))
        bits |= PERMISSION_BITS[key]
        names.append(key)
    return bits, tuple(names)


def parse_template(raw: str) -> ServerTemplate:
    """YAML/JSON text → a validated ServerTemplate. TemplateError if invalid."""
    if not isinstance(raw, str) or not raw.strip():
        raise TemplateError(t("template.err_empty_template"))
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TemplateError(t("template.err_yaml_parse", exc=exc)) from exc
    if not isinstance(data, dict):
        raise TemplateError(t("template.err_top_level_mapping"))

    roles_raw = data.get("roles") or []
    cats_raw = data.get("categories") or []
    if not isinstance(roles_raw, list):
        raise TemplateError(t("template.err_roles_list"))
    if not isinstance(cats_raw, list):
        raise TemplateError(t("template.err_categories_list"))
    if not roles_raw and not cats_raw:
        raise TemplateError(t("template.err_need_one"))
    if len(roles_raw) > MAX_ROLES:
        raise TemplateError(t("template.err_too_many_roles", max=MAX_ROLES))
    if len(cats_raw) > MAX_CATEGORIES:
        raise TemplateError(t("template.err_too_many_categories", max=MAX_CATEGORIES))

    roles: list[TemplateRole] = []
    seen_roles: set[str] = set()
    for i, r in enumerate(roles_raw):
        if not isinstance(r, dict):
            raise TemplateError(t("template.err_role_mapping", i=i))
        name = _req_str(r.get("name"), f"roles[{i}].name")
        if name in seen_roles:
            raise TemplateError(t("template.err_dup_role", name=name))
        seen_roles.add(name)
        bits, names = _perm_bits(r.get("permissions"), f"roles[{i}]")
        roles.append(TemplateRole(name=name, permission_bits=bits, permission_names=names))

    cats: list[TemplateCategory] = []
    seen_cats: set[str] = set()
    for i, c in enumerate(cats_raw):
        if not isinstance(c, dict):
            raise TemplateError(t("template.err_category_mapping", i=i))
        cname = _req_str(c.get("name"), f"categories[{i}].name")
        if cname in seen_cats:
            raise TemplateError(t("template.err_dup_category", name=cname))
        seen_cats.add(cname)
        private = bool(c.get("private", False))
        visible_raw = c.get("visible_to") or []
        if not isinstance(visible_raw, list):
            raise TemplateError(t("template.err_visible_to_list", i=i))
        visible = tuple(_req_str(v, f"categories[{i}].visible_to[]") for v in visible_raw)
        chans_raw = c.get("channels") or []
        if not isinstance(chans_raw, list):
            raise TemplateError(t("template.err_channels_list", i=i))
        if len(chans_raw) > MAX_CHANNELS_PER_CATEGORY:
            raise TemplateError(
                t("template.err_too_many_channels", i=i, max=MAX_CHANNELS_PER_CATEGORY)
            )
        chans: list[TemplateChannel] = []
        for j, ch in enumerate(chans_raw):
            if not isinstance(ch, dict):
                raise TemplateError(t("template.err_channel_mapping", i=i, j=j))
            chname = _req_str(ch.get("name"), f"categories[{i}].channels[{j}].name")
            kind = ch.get("type") or "text"
            if not isinstance(kind, str) or kind.strip().lower() not in CHANNEL_KINDS:
                raise TemplateError(
                    t("template.err_channel_type", i=i, j=j, kind=kind)
                )
            chans.append(TemplateChannel(name=chname, kind=kind.strip().lower()))
        cats.append(
            TemplateCategory(
                name=cname, channels=tuple(chans), private=private, visible_to=visible
            )
        )

    return ServerTemplate(roles=tuple(roles), categories=tuple(cats))


def to_yaml(template: ServerTemplate) -> str:
    """ServerTemplate → YAML string. A format that parse_template can read back (round-trip)."""
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
