You tried to call a tool named "{{ tool_name }}", but no such tool exists, so nothing was executed.

{% if available_tools %}The only tools available to you are: {{ available_tools }}.{% else %}You currently have no tools available.{% endif %}

Do not call "{{ tool_name }}" again. Answer the user directly using what you already know, or call one of the available tools listed above.
