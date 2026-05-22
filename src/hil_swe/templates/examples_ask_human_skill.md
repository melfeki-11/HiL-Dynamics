---
name: clarify-information
description: |-
    Use this skill for coding tasks with underspecified problem statements.
    Use this skill to get clarification for an implementation detail that is
    unclear, confusing, contradictory, or missing, and there is no explicit
    answer in the problem statement or codebase. Even implicit answers from
    the codebase are not good enough.
---

First identify what is the missing piece of information, ambiguous information, or contradictory information present in the task that you need to clarify. Then, use a question-asking tool you have at your disposal to ask a human user a clarification question.

Rules for asking:
- Submit only ONE, clear, specific question at a time, targeting one specific detail.
- Never ask multiple questions in one tool call. If there are multiple details you want to clarify, you MUST use this skill multiple times, asking questions one by one. Otherwise, the user won't answer.
- Never ask general questions about high-level or even medium-level implementation details. E.g. "How should I implement function X?" is a bad question that will NOT be answered by the human user. A much more specific one, such as, "What is the expected return type of function X?" CAN be answered by the human.
- If the user deems your question irrelevant, but you believe it's a necessary clarification, try asking again but reword, structure, or format your question differently. An user response of "irrelevant question" doesn't just come from asking a useless question; it could also be because you did not ask a specific-enough question, or because you put more than one question in one tool call.
- If the human answers your question, **do not ask about the same detail again.** Always immediately incorporate their clarification into your code changes.
- Always integrate previous human user answers into your problem solving process to unblock you in your implementation or so you can ask follow-up questions.
