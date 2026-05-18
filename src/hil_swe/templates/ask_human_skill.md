---
name: clarify-information
description: |-
    Use this skill when a task cannot be completed correctly because a decision-critical requirement is missing, ambiguous, or contradictory after you inspect the available task text, repository, tests, schemas, logs, or other local evidence.
---

First inspect the task and the directly relevant local evidence. If a required decision still cannot be inferred, ask a human collaborator one concise clarification question using `{{TOOL_NAME}}`.

Rules for asking:
- Ask only when the missing information affects the implementation, query, configuration, or final answer.
- Ask one question per tool call, focused on one concrete decision.
- Anchor the question in the artifact you inspected, such as a function name, file path, schema field, setting name, user-facing behavior, or observed contradiction.
- Prefer questions that can be answered with a fact, a threshold, a chosen option, or a short rule.
- Do not ask for broad design advice, implementation walkthroughs, or information that another local read/search/test can answer.
- When the human answers, incorporate that answer and continue. Do not ask again about the same decision unless the answer creates a new concrete ambiguity.

Examples:
- Software: "In `parse_config()`, if `timeout_ms` is omitted, should the default be 0, 1000, or inherited from the parent config?"
- Data analysis: "For the `net_revenue` column, should refunds be subtracted before or after currency normalization?"
- SQL: "When an account has multiple active plans on the same day, should the query select the newest plan or count all active plans?"
- Infrastructure: "For the staging deploy, should `FEATURE_X_ENABLED` default to false until rollout approval, or match production?"
- Product requirement: "If two records tie for the top score, should the UI show both records or apply a deterministic tie-breaker?"
