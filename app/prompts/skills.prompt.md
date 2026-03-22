## Available Skills

The following skills extend your capabilities.

To use a skill, read its SKILL.md file using the `read_file` tool.

{% for skill in skills %}
### {{ skill.name }}
{{ skill.description }}
{{ skill.location}}
{% endfor %}
