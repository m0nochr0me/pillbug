{{ agents_md }}
{% if skills %}

---

{{ skills }}
{% endif %}
{% if channel_memos %}

---

{% for memo in channel_memos %}
{{ memo }}
{% if not loop.last %}

---
{% endif %}
{% endfor %}
{% endif %}

---

{{ base_context }}
