{% if auto_skills %}
## Active Skills

These skill instructions are always in effect.
{% for skill in auto_skills %}
### {{ skill.name }}
{{ skill.body }}
{% endfor %}
{% endif %}
{% if auto_skills and ondemand_skills %}

---

{% endif %}
{% if ondemand_skills %}
## Available Skills

The following skills extend your capabilities.

To use a skill, read its SKILL.md file using the `read_file` tool.
{% for skill in ondemand_skills %}
### {{ skill.name }}
{{ skill.description }}
{{ skill.location }}
{% endfor %}
{% endif %}
