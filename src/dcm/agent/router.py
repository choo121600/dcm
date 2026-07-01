"""NL router over a closed verb set (ralplan S3).

Forces the LLM into a single tool_use call to extract verb+params without free-form generation.
Every privileged dispatch passes through the single chokepoint (route()) in this module.
No discord import — uses only platform/base.AuthContext.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..i18n import t
from ..platform.base import AuthContext, is_admin

if TYPE_CHECKING:
    from ..llm import LLMClient
    from ..service.guild_admin import GuildAdminService

log = logging.getLogger(__name__)

# The actual verb set of GuildAdminService + a fallback sentinel.
VERBS: list[str] = [
    "create_category",
    "create_channel",
    "edit_channel",
    "delete_channel",
    "create_role",
    "assign_role",
    "remove_role",
    "set_role_permissions",
    "create_project",
    "kick",
    "ban",
    "timeout",
    "purge",
    "none",
]

# anthropic tool definition — name must be 'dispatch' (forced via tool_choice).
DISPATCH_TOOL: dict = {
    "name": "dispatch",
    "description": (
        "Parse the user's natural-language message and map it to a Discord server-management "
        "action. If there is no clear management intent, or it is uncertain, set verb to 'none'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verb": {
                "type": "string",
                "enum": VERBS,
                "description": (
                    "The management action to run, or 'none' if not applicable. "
                    "Possible values: create_category, create_channel, edit_channel, "
                    "delete_channel, create_role, assign_role, remove_role, "
                    "set_role_permissions, create_project, kick, ban, timeout, purge, none."
                ),
            },
            "params": {
                "type": "object",
                "description": "Parameters the verb needs; the fields vary by verb.",
            },
        },
        "required": ["verb"],
    },
}

# Destructive verbs: on the NL path, return only a confirmation notice without calling the service (simple S3 handling; to be refined in S5).
_DESTRUCTIVE_VERBS: frozenset[str] = frozenset({"delete_channel", "create_project"})

# Required params per verb, checked before calling the service.
_REQUIRED_PARAMS: dict[str, list[str]] = {
    "create_category": ["name"],
    "create_channel": ["name", "kind"],
    "edit_channel": ["channel_id"],
    "create_role": ["name"],
    "assign_role": ["user_id", "role_id"],
    "remove_role": ["user_id", "role_id"],
    "set_role_permissions": ["role_id", "permissions"],
    "kick": ["user_id"],
    "ban": ["user_id"],
    "timeout": ["user_id", "duration"],
    "purge": ["channel_id", "count"],
}

# Placeholder/empty values the model inserts when it doesn't know a value — treated as missing to avoid generating a bogus name.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    {"", "<unknown>", "unknown", "none", "null", "n/a", "미정", "없음", "이름", "제목"}
)

# Parser instructions are in English: they steer the model, which parses user input in any
# language (the closed verb set and placeholder guards below are language-agnostic).
_DISPATCH_SYSTEM = (
    "You are a parser for Discord server-management commands. "
    "Analyze the user's message and call the single dispatch tool. "
    "Supported actions: create_category, create_channel, edit_channel, delete_channel, "
    "create_role, assign_role, remove_role, set_role_permissions, create_project, "
    "kick, ban, timeout, purge. "
    "If there is no server-management intent, return verb='none'. "
    "Put only the values the action needs into params, and never guess a value you weren't given. "
    "If the user did not state a required value such as a name, omit that field entirely "
    "(no empty strings or placeholders like <UNKNOWN>)."
)


class NLRouter:
    """Shallow NL router over a closed verb set — the single privileged dispatch chokepoint.

    If route() returns not None, the orchestrator uses that result as the final response;
    if it returns None, it falls back to the persona chat path.
    """

    def __init__(
        self,
        llm: LLMClient,
        service: GuildAdminService,
        dispatch_model: str | None = None,
    ) -> None:
        self._llm = llm
        self._service = service
        # Classification (dispatch) uses the cheap model — this path runs on every mention, so it's costly. None means the default model.
        self._dispatch_model = dispatch_model

    async def route(self, auth: AuthContext, user_text: str) -> str | None:
        """Parse the NL text and return the admin-command execution result, or None to fall back.

        Every privileged verb must pass the single is_admin check in this method.
        """
        extracted = await self._llm.extract_dispatch(
            _DISPATCH_SYSTEM,
            user_text,
            tool=DISPATCH_TOOL,
            model=self._dispatch_model,
        )
        if extracted is None:
            return None

        verb: str = extracted.get("verb") or "none"
        if verb == "none" or verb not in VERBS:
            return None

        # Multi-guild: admin commands act only on the context guild — reject (fail-closed) when there's no guild (a DM, in theory).
        if not auth.guild_id:
            return t("router.guild_only")

        # ── single privileged dispatch chokepoint ──────────────────────────
        if not is_admin(auth.role_ids, auth.admin_role_id, auth.is_owner, auth.has_manage_guild):
            return t("router.admin_only")

        params: dict = extracted.get("params") or {}

        # Destructive action: return a confirmation notice without calling the service (no direct execution on the NL path).
        if verb in _DESTRUCTIVE_VERBS:
            return t("router.destructive_blocked", verb=verb)

        # Validate required params — if any are missing, ask for confirmation instead of guessing.
        required = _REQUIRED_PARAMS.get(verb, [])
        missing = [
            k
            for k in required
            if k not in params
            or params[k] is None
            or (isinstance(params[k], str) and params[k].strip().lower() in _PLACEHOLDER_VALUES)
        ]
        if missing:
            return t("router.missing_params", verb=verb, missing=", ".join(missing))

        return await self._dispatch(verb, auth, params)

    async def _dispatch(self, verb: str, auth: AuthContext, params: dict) -> str:
        """Map the verb to a GuildAdminService method, call it, and return the result in Korean."""
        common = dict(
            guild_id=auth.guild_id,
            actor_name=auth.author_name,
            actor_id=auth.author_id,
        )
        try:
            if verb == "create_category":
                result = await self._service.create_category(**common, name=params["name"])
            elif verb == "create_channel":
                result = await self._service.create_channel(
                    **common,
                    name=params["name"],
                    kind=params["kind"],
                    category_id=params.get("category_id"),
                )
            elif verb == "edit_channel":
                result = await self._service.edit_channel(
                    **common,
                    channel_id=params["channel_id"],
                    name=params.get("name"),
                    category_id=params.get("category_id"),
                )
            elif verb == "create_role":
                result = await self._service.create_role(
                    **common,
                    name=params["name"],
                    permissions=params.get("permissions", 0),
                )
            elif verb == "assign_role":
                result = await self._service.assign_role(
                    **common,
                    user_id=params["user_id"],
                    role_id=params["role_id"],
                )
            elif verb == "remove_role":
                result = await self._service.remove_role(
                    **common,
                    user_id=params["user_id"],
                    role_id=params["role_id"],
                )
            elif verb == "set_role_permissions":
                result = await self._service.set_role_permissions(
                    **common,
                    role_id=params["role_id"],
                    permissions=params["permissions"],
                )
            elif verb == "kick":
                result = await self._service.kick_member(**common, user_id=params["user_id"])
            elif verb == "ban":
                result = await self._service.ban_member(**common, user_id=params["user_id"])
            elif verb == "timeout":
                result = await self._service.timeout_member(
                    **common,
                    user_id=params["user_id"],
                    duration_seconds=int(params["duration"]),
                )
            elif verb == "purge":
                result = await self._service.purge_messages(
                    **common,
                    channel_id=params["channel_id"],
                    count=int(params["count"]),
                )
            else:
                log.warning("NLRouter: unknown verb=%s", verb)
                return t("router.unknown_verb", verb=verb)
        except Exception:
            log.exception("NLRouter dispatch error (verb=%s)", verb)
            return t("router.dispatch_error")

        # Convert the service result.
        if result.needs_confirmation:
            return t("router.needs_confirmation", verb=verb)
        if result.ok:
            return t("router.ok", detail=result.detail)
        return t("router.fail", detail=result.detail)
