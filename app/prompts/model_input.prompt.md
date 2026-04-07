{{ base_context }}

{{ agents_md }}
{% if channel_memos %}

---

{% for memo in channel_memos %}
{{ memo }}
{% if not loop.last %}

---
{% endif %}
{% endfor %}
{% endif %}
{% if todo_list %}

---

{{ todo_list }}
{% endif %}
{% if skills %}

---

{{ skills }}
{% endif %}
