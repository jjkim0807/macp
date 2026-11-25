---
tags:
{{tags}}
status: open
due: "-"
updated: {{today}}
priority: 50
repo: "{{repo}}"
worker_mode: {{worker_mode}}
auto_merge: false
outcome: "{{outcome}}"
next_action: "{{next_action}}"
if_then: "{{if_then}}"
estimate_p50: {{estimate_p50}}
estimate_p90: {{estimate_p90}}
review_cycle: "-"
acceptance_criteria:
{{acceptance_criteria}}
touches:
{{touches}}
---

# {{title}}

## Spec

- Goal: {{outcome}}
- Request, as typed: {{request}}
{{references}}- `auto_merge: false` is the `macp add` default: a person reviews and merges the result. It is not a safety or finance marker.

## Progress Log

- {{today}}: created by `macp add`.
