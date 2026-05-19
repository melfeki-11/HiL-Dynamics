---
name: clarify-information
description: |-
    Use this skill for coding tasks with underspecified problem statements. Use this skill to get clarification for an implementation detail that is unclear, confusing, contradictory, or missing, and there is no explicit answer in the problem statement or codebase. Even implicit answers from the codebase are not good enough.

    Example situations for when you should use this skill:
    1. The task asks to implement a timeout, but the exact value is not specified. While there are other timeout values in the codebase, this specific one is not explicitly provided. You must use this skill to get human clarification.
    2. The problem says a specific permission should be applied to a post if it satisfies an `isValid` condition, but it's ambiguous what counts as valid, what attributes to look at to determine validity, what filters to apply, etc. Instead of making a guess, you must use this skill to get human clarification.
    3. One part of the problem says to follow existing formatting rules in the module, while another part says to implement a new paradigm. These two are direct contradictions. Instead of choosing one option to go with, you must use this skill to get human clarification.
    4. The task requires storing a value, but there are two possible sources to get this value. While one might be more commonly used than the other, in general or even in the specific codebase, it's not explicitly provided and so you must use this skill to get human clarification.
    5. The task asks to display the product with the highest clicks, but does not say what to do in the edge case of having multiple products tied for highest clicks. Instead of displaying both, which is neither explicitly encouraged nor discouraged, you must use this skill to get human clarification.
    6. The task says you must ensure all IDs are stored as-is, with no formatting changes, but a downstream function in the codebase implements blanket formatting changes. Since there is a contradiction here with no explicit resolution in the problem or code, you must use this skill to get human clarification.
---

First identify what is the missing piece of information, ambiguous information, or contradictory information present in the task that you need to clarify. Then, use a question-asking tool you have at your disposal to ask a human user a clarification question.

Rules for asking for help:
- Submit only ONE, clear, specific question at a time, targeting one specific detail.
- Never ask multiple questions in one tool call. If there are multiple details you want to clarify, you MUST use this skill multiple times, asking questions one by one. Otherwise, the user won't answer.
- Never ask general questions about high-level or even medium-level implementation details. E.g. "How should I implement function X?" is a bad question that will NOT be answered by the human user. A much more specific one, such as, "What is the expected return type of function X?" CAN be answered by the human.
- If the user deems your question irrelevant, but you believe it's a necessary clarification, try asking again but reword, structure, or format your question differently. An user response of "irrelevant question" doesn't just come from asking a useless question; it could also be because you did not ask a specific-enough question, or because you put more than one question in one tool call.
- If the human answers your question, **do not ask about the same detail again.** Always immediately incorporate their clarification into your code changes.
- Always integrate previous human user answers into your problem solving process to unblock you in your implementation or so you can ask follow-up questions.
