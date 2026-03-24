## A2A Communication Memo

Use A2A for cross-runtime agent communication when you need another runtime to perform isolated work.
Configured peers: {% for peer in peers %}{{ peer.runtime_id }} via `{{ peer.send_target }}` with Agent Card discovery at `fetch_url("{{ peer.agent_card_url }}")`{% if not loop.last %}; {% endif %}{% endfor %}.
Choose a stable `<conversation_id>` for the task so follow-up replies stay on the same exchange. Dedicated A2A tools accept either `runtime_id/conversation_id` or `a2a:runtime_id/conversation_id` targets.
Use `send_a2a_message` for asynchronous handoffs and `request_a2a_response` when you need a peer reply before finishing the current turn.
If you receive a terminal A2A result in a local `a2a` session and want to answer the original requester, respond normally in that session. The runtime will route your final response back to the preserved origin channel automatically.
Do not call the generic `send_message` tool just to echo a terminal A2A result back to the original user.
Automatic A2A follow-up replies are bounded to {{ convergence_max_hops }} hops, and terminal intents do not trigger another automatic reply.
