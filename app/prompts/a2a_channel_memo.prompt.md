## A2A Communication Memo

Use A2A for cross-runtime agent communication when you need another runtime to perform isolated work.
Configured peers: {% for peer in peers %}{{ peer.runtime_id }} via `{{ peer.send_target }}` with Agent Card discovery at `fetch_url("{{ peer.agent_card_url }}")`{% if not loop.last %}; {% endif %}{% endfor %}.
Choose a stable `<conversation_id>` for the task so follow-up replies stay on the same exchange.
Automatic A2A follow-up replies are bounded to {{ convergence_max_hops }} hops, and terminal intents do not trigger another automatic reply.
