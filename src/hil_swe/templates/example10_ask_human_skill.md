---
name: clarify-information
description: |-
    Use this skill for coding tasks with underspecified problem statements.
    Use this skill to get clarification for an implementation detail that is
    unclear, confusing, contradictory, or missing, and there is no explicit
    answer in the problem statement or codebase. Even implicit answers from
    the codebase are not good enough.
---

Before each ask, do two things silently in your reasoning:
1. **Enumerate every open blocker** you currently know about (missing value, ambiguous condition, conflict, unspecified edge case). Brief mental list — no need to write it out.
2. **Pick the highest-impact blocker that you have not already asked about.** Skip anything you've asked even if you'd rephrase it.

Rules for asking:
- Submit only ONE, clear, specific question at a time, targeting one specific detail.
- Never ask multiple questions in one tool call. If there are multiple details you want to clarify, you MUST use this skill multiple times, asking questions one by one. Otherwise, the user won't answer.
- Anchor every question to a specific artifact: name the function, file path, schema field, test name, or observed behavior it concerns.
- Never ask general questions about high-level or even medium-level implementation details. E.g. "How should I implement function X?" is a bad question that will NOT be answered by the human user. A much more specific one, such as, "What is the expected return type of function X?" CAN be answered by the human.
- **If multiple candidate questions about the same function, file, schema, or test could be answered together by understanding one underlying decision, COMBINE them into a single question that asks the canonical decision.** For example, asking *"what's the return type?"* + *"what's the error case?"* + *"what's the format?"* for `fetchUser()` is one underlying question: *"what is the full signature and error contract for fetchUser?"* Asking related questions separately wastes your budget and lowers your precision.
- If the user deems your question irrelevant, but you believe it's a necessary clarification, try asking again but reword, structure, or format your question differently. An user response of "irrelevant question" doesn't just come from asking a useless question; it could also be because you did not ask a specific-enough question, or because you put more than one question in one tool call.
- If the human answers your question, **do not ask about the same detail again.** Always immediately incorporate their clarification into your code changes.
- Always integrate previous human user answers into your problem solving process to unblock you in your implementation or so you can ask follow-up questions.
