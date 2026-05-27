"""Gemini wire-format proxy that fronts Claude via the official Anthropic SDK.

Unlike the sibling `pillbug-claude-proxy`, which drives the local Claude Code
CLI through `claude-agent-sdk`, this variant calls `anthropic.AsyncAnthropic`
directly. Authentication uses the Claude Code subscription OAuth token
(`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`) so requests are still
billed against the user's Claude Pro/Max subscription. Multi-turn history
flows through as native Anthropic messages with structured `tool_use` and
`tool_result` blocks — no agent loop, no system-prompt transcript bridge,
no pseudo-XML leakage.
"""
