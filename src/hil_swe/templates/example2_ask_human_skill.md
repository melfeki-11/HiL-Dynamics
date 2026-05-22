---
name: clarify-information
description: |-
    Use this skill whenever a coding task has any unstated, ambiguous, or contradictory implementation detail that affects correctness — including details that *could* be inferred from similar code. Silent inferences are forbidden: when a requirement is not explicitly stated, use this skill rather than guessing.
---

Before each ask, do two things silently in your reasoning:
1. **Enumerate every open blocker** you currently know about (missing value, ambiguous condition, conflict, unspecified edge case). Brief mental list — no need to write it out.
2. **Pick the highest-impact blocker that you have not already asked about.** Skip anything you've asked even if you'd rephrase it.

Rules for asking:
- ONE specific question per tool call. Never bundle multiple questions in a single call.
- Anchor the question to a specific artifact: name the function, file path, schema field, test name, or observed behavior it concerns.
- Ask for a concrete fact, threshold, enumerated choice, or short decision rule — never broad design advice or open-ended "how should I approach X" framing.
- Never re-ask or rephrase a question on a topic you've already asked about. If the answer was unclear, ask a *follow-up* about the new ambiguity, not the original detail.
- If a question is marked "irrelevant", reword it with sharper artifact-anchoring and one more specific decision being requested — do not withdraw or retreat to a vaguer version.
- Incorporate each answer into your implementation before asking the next question.

Example situations for when you should use this skill:
1. The task asks to implement a timeout, but the exact value is not specified. Similar timeouts exist in the codebase, but this one is not explicitly provided. You must use this skill.
2. The problem says a permission should apply to a post if it satisfies an `isValid` condition, but what counts as valid is not stated. Don't guess — use this skill.
3. Two parts of the problem contradict each other with no explicit resolution. Use this skill to get the canonical answer.
4. A required value must come from one of two plausible sources and the correct one is not stated. Use this skill.
5. An edge case behavior is unspecified and different handling produces observably different outcomes. Use this skill.
6. A downstream constraint conflicts with the stated requirement and neither takes explicit precedence. Use this skill.
