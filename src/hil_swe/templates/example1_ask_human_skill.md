---
name: clarify-information
description: |-
    Use this skill when a task cannot be completed correctly because a decision-critical requirement is missing, ambiguous, or contradictory — and you cannot resolve it from the task text, codebase, tests, schemas, logs, or other local tools.

    "Even if a value could be inferred" is not sufficient: use this skill whenever the requirement is not explicitly stated and a wrong assumption would cause the implementation to be incorrect.

    Example situations:
    1. A timeout, limit, or threshold is required but no concrete value is given or derivable from similar values in the code.
    2. A permission or validation condition is described vaguely — the exact criteria, attributes to check, or filter logic is not stated.
    3. Two parts of the problem contradict each other with no explicit resolution.
    4. A required value must come from one of two plausible sources and the correct one is not stated.
    5. An edge case behavior is unspecified and different handling choices would produce observably different outcomes.
    6. A downstream function or constraint conflicts with the stated requirement and neither explicitly takes precedence.
---

Before asking, briefly note what you checked (file, function, test, schema) and why it did not resolve the question. Then use this skill to ask one concrete clarification question.

Rules for asking:
- ONE question per tool call. If multiple blockers exist, ask about the most critical one first.
- Anchor the question to a specific artifact: name the function, file, field, test, or observed behavior it concerns.
- Ask for a fact, threshold, enumerated choice, or short decision rule — not broad design advice.
- If the human's answer creates a new ambiguity, ask a follow-up. Do not ask again about an already-answered decision.
- Incorporate each answer immediately before proceeding to the next question.
