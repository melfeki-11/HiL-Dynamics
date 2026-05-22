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
- **If two of your candidate questions concern the same function, file, schema field, or test, they are fragments of one underlying decision. COMBINE them into a single question.** Asking two questions about the same artifact wastes your budget.
- If a question is marked "irrelevant", reword it with sharper artifact-anchoring and one more specific decision being requested — do not withdraw or retreat to a vaguer version.
- Incorporate each answer into your implementation before asking the next question.
- **Stop after 4 questions per pass.** If you have more than 4 candidates, you are including non-blockers — re-filter: would skipping this question force a guess that could break the implementation?
