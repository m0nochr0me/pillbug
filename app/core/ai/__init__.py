"""
AI client.

Package layout: GeminiChatService in service.py, GeminiChatSession in
session.py, inbound MIME/attachment helpers in attachments.py.
"""

# _load_agents_md_cached/_read_agents_md/_UNKNOWN_TOOL_MAX_NUDGES/ChatResponse are
# re-exported because tests reach them as attributes of `app.core.ai`.
from app.core.ai.attachments import resolve_inbound_attachment_path as resolve_inbound_attachment_path
from app.core.ai.service import GeminiChatService, chat_service
from app.core.ai.service import _load_agents_md_cached as _load_agents_md_cached
from app.core.ai.service import _read_agents_md as _read_agents_md
from app.core.ai.session import _UNKNOWN_TOOL_MAX_NUDGES as _UNKNOWN_TOOL_MAX_NUDGES
from app.core.ai.session import GeminiChatSession
from app.schema.ai import ChatResponse as ChatResponse

__all__ = (
    "GeminiChatService",
    "GeminiChatSession",
    "chat_service",
)
