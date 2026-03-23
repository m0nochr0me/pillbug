"""Helpers for building A2A Agent Card discovery payloads."""

import re

from app import __version__
from app.core.config import settings
from app.schema.agent_card import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HttpAuthSecurityScheme,
    SecurityScheme,
)
from app.schema.messages import A2A_CONVERGENCE_EXTENSION_URI
from app.util.skills import discover_workspace_skills

_CUSTOM_BINDING_NAME = "PILLBUG-A2A"
_PROTOCOL_VERSION = "1.0"
_TERMINAL_INTENTS = ("result", "inform", "error", "heartbeat")
_RESPONSE_ELIGIBLE_INTENTS = ("ask", "delegate")
_DEFAULT_INPUT_MODES = ("text/plain",)
_DEFAULT_OUTPUT_MODES = ("text/plain",)


def _normalized_base_url() -> str:
    configured_base_url = (settings.A2A_SELF_BASE_URL or "").strip()
    if configured_base_url:
        return configured_base_url.rstrip("/")

    return settings.mcp_shortener_base_url().rstrip("/")


def _provider() -> AgentProvider | None:
    organization = settings.A2A_PROVIDER_ORGANIZATION.strip()
    provider_url = (settings.A2A_PROVIDER_URL or "").strip()
    if not organization or not provider_url:
        return None

    return AgentProvider(organization=organization, url=provider_url)


def _security_schemes() -> tuple[dict[str, SecurityScheme] | None, tuple[dict[str, tuple[str, ...]], ...] | None]:
    if settings.a2a_bearer_token() is None:
        return None, None

    scheme_name = "a2aBearer"
    return (
        {
            scheme_name: SecurityScheme(
                http_auth_security_scheme=HttpAuthSecurityScheme(
                    scheme="Bearer",
                    bearer_format="Opaque",
                    description="Bearer token required for authenticated Pillbug A2A peer access.",
                )
            )
        },
        ({scheme_name: ()},),
    )


def _communication_rules_extension(*, include_examples: bool) -> AgentExtension:
    params: dict[str, object] = {
        "maxHops": settings.A2A_CONVERGENCE_MAX_HOPS,
        "responseEligibleIntents": list(_RESPONSE_ELIGIBLE_INTENTS),
        "terminalIntents": list(_TERMINAL_INTENTS),
        "onLimitReached": "return terminal notice and stop automatic replies",
        "noAutomaticReplyOnTerminalIntent": True,
    }
    if include_examples:
        params["stopNoticeExample"] = (
            "Convergence limit reached for this A2A exchange. No further automatic replies will be sent."
        )

    return AgentExtension(
        uri=A2A_CONVERGENCE_EXTENSION_URI,
        description="Declares Pillbug's bounded autonomous exchange rules for cross-runtime messaging.",
        required=False,
        params=params,
    )


def _skill_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return normalized.strip("-") or "skill"


def _builtin_skills(*, include_extended_examples: bool) -> tuple[AgentSkill, ...]:
    channel_examples = [
        "Send the final handoff to a2a:runtime-b/deploy-42 only if a new cross-runtime exchange is required.",
        "Notify telegram:123456789 after the scheduled verification completes.",
    ]
    if include_extended_examples:
        channel_examples.append(
            "If you receive a terminal result, stop automatic replies and only use an outbound message tool when you need to start a fresh exchange."
        )

    return (
        # AgentSkill(
        #     id="workspace-inspection",
        #     name="Workspace Inspection",
        #     description=(
        #         "Inspects the isolated runtime workspace by listing directories, reading files, and searching for "
        #         "matching paths or text."
        #     ),
        #     tags=("workspace", "files", "search", "analysis"),
        #     examples=(
        #         "Read app/runtime/loop.py and explain how inbound batching works.",
        #         "Find where scheduled tasks are persisted and summarize the data model.",
        #     ),
        #     input_modes=_DEFAULT_INPUT_MODES,
        #     output_modes=_DEFAULT_OUTPUT_MODES,
        # ),
        # AgentSkill(
        #     id="workspace-editing",
        #     name="Workspace Editing",
        #     description="Creates new UTF-8 files and applies targeted text changes within the isolated workspace.",
        #     tags=("workspace", "editing", "files", "automation"),
        #     examples=(
        #         "Patch this module so empty task names are rejected before saving.",
        #         "Add the new environment variable to the deployment README.",
        #     ),
        #     input_modes=_DEFAULT_INPUT_MODES,
        #     output_modes=_DEFAULT_OUTPUT_MODES,
        # ),
        # AgentSkill(
        #     id="command-execution",
        #     name="Command Execution",
        #     description=(
        #         "Runs bounded shell commands inside the workspace and returns captured stdout, stderr, exit codes, "
        #         "and timeout state."
        #     ),
        #     tags=("workspace", "shell", "validation", "automation"),
        #     examples=(
        #         "Run the lint command and report only the blocking failures.",
        #         "List the optional uv extras configured for this repository.",
        #     ),
        #     input_modes=_DEFAULT_INPUT_MODES,
        #     output_modes=_DEFAULT_OUTPUT_MODES,
        # ),
        AgentSkill(
            id="web-context-fetching",
            name="Web Context Fetching",
            description=(
                "Fetches remote resources into the workspace and converts HTML into readable markdown when possible "
                "for later analysis."
            ),
            tags=("web", "research", "context", "fetch"),
            examples=(
                "Fetch this incident report URL and summarize the required operator actions.",
                "Download the API reference page so you can compare it with our implementation.",
            ),
            input_modes=_DEFAULT_INPUT_MODES,
            output_modes=_DEFAULT_OUTPUT_MODES,
        ),
        AgentSkill(
            id="planning-and-scheduling",
            name="Planning And Scheduling",
            description=(
                "Maintains session-scoped todo plans and manages scheduled background agent tasks for delayed or "
                "recurring follow-up work."
            ),
            tags=("planning", "todo", "scheduler", "automation"),
            examples=(
                "Break this migration into a short todo list with one in-progress step.",
                "Create a delayed background task to re-check runtime health in 30 minutes.",
            ),
            input_modes=_DEFAULT_INPUT_MODES,
            output_modes=_DEFAULT_OUTPUT_MODES,
        ),
        AgentSkill(
            id="cross-runtime-delegation",
            name="Cross-Runtime Delegation",
            description=(
                "Coordinates bounded cross-runtime or channel-based follow-up by sending direct outbound messages to "
                "configured destinations when a new exchange is required."
            ),
            tags=("a2a", "channels", "delegation", "handoff"),
            examples=tuple(channel_examples),
            input_modes=_DEFAULT_INPUT_MODES,
            output_modes=_DEFAULT_OUTPUT_MODES,
        ),
    )


def _workspace_agent_skills() -> tuple[AgentSkill, ...]:
    return tuple(
        AgentSkill(
            id=f"workspace-skill-{_skill_id(skill.name)}",
            name=skill.name,
            description=skill.description,
            tags=("workspace-skill", "custom"),
            input_modes=_DEFAULT_INPUT_MODES,
            output_modes=_DEFAULT_OUTPUT_MODES,
        )
        for skill in discover_workspace_skills(settings.WORKSPACE_ROOT)
    )


def _skills(*, include_extended_examples: bool) -> tuple[AgentSkill, ...]:
    discovered_skills = _workspace_agent_skills()
    combined_skills = _builtin_skills(include_extended_examples=include_extended_examples) + discovered_skills
    unique_skills: dict[str, AgentSkill] = {}

    for skill in combined_skills:
        unique_skills.setdefault(skill.id, skill)

    return tuple(unique_skills.values())


def _card(*, include_extended_examples: bool) -> AgentCard:
    base_url = _normalized_base_url()
    security_schemes, security = _security_schemes()
    description = (
        settings.A2A_AGENT_DESCRIPTION or ""
    ).strip() or f"Isolated Pillbug runtime {settings.runtime_id} for bounded cross-runtime collaboration."

    return AgentCard(
        name=(settings.AGENT_NAME or settings.runtime_id).strip(),
        description=description,
        supported_interfaces=(
            AgentInterface(
                url=f"{base_url}{settings.A2A_INGRESS_PATH}",
                protocol_binding=_CUSTOM_BINDING_NAME,
                protocol_version=_PROTOCOL_VERSION,
            ),
        ),
        provider=_provider(),
        version=__version__,
        documentation_url=((settings.A2A_DOCUMENTATION_URL or "").strip() or None),
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
            extended_agent_card=settings.a2a_bearer_token() is not None,
            extensions=(_communication_rules_extension(include_examples=include_extended_examples),),
        ),
        security_schemes=security_schemes,
        security=security,
        default_input_modes=_DEFAULT_INPUT_MODES,
        default_output_modes=_DEFAULT_OUTPUT_MODES,
        skills=_skills(include_extended_examples=include_extended_examples),
        icon_url=((settings.A2A_ICON_URL or "").strip() or None),
    )


def build_public_agent_card() -> AgentCard:
    return _card(include_extended_examples=False)


def build_extended_agent_card() -> AgentCard | None:
    if settings.a2a_bearer_token() is None:
        return None

    return _card(include_extended_examples=True)
