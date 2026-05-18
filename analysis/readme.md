# HIL-Bench Model-Harness Narrative Working Draft

This is a rough coauthor-facing draft, not final prose. The goal is to capture the argument we are converging on quickly enough that others can revise, correct labels, and decide which figures to keep.


The original HIL-Bench paper established a selective-escalation failure mode under a controlled evaluation harness: agents can solve many tasks when missing information is supplied up front, but recover only a fraction of that performance when they must decide whether and when to ask for help.

However, many harnesses are much stronger than the SWE-agent one we used before. Native harnesses such as Claude Code or Codex are reported to be very strong. Moreover, within each harness, there can be different policies such as always-say-yes or discouraging asking. We then follow up on HIL-Bench with a different question: do models still fail with stronger harnesses and policies?

The current evidence suggests that HIL-Bench should be read as a benchmark of the coupled `<model, harness, policy>` system, not the model alone. Stronger native harnesses and policy variants do change behavior. They change how often agents ask and when they ask. These variations, however, do not make the underlying selective-escalation gap vanish.

[TODO add blurb about the prompting changes that help]


1. HIL-Bench remains hard even for what the community thinks are stronger harnesses.
2. Harness and policy design visibly changes the failure mode.
3. [prompt edit stuff tbd]

## Paper Context: What HIL-Bench Already Showed

The paper result was intentionally clean, albeit simple. In the SWE setting, the harness was held mostly fixed around SWE-agent, and the experiment varied models. That gave us a scientific baseline: the same style of interactive workflow exposed a broad judgment gap across frontier systems.

The key observation was not just that agents sometimes fail coding tasks. It was that they fail in a particular way. With full information, many tasks become solvable. With `ask_human()` available, agents still often fail to notice that the missing information is task-critical, ask too broadly or too early, ask but fail to use the answer, or commit to an underspecified implementation.

The paper framing is still the reminder we should keep in the intro:

> HIL-Bench measures selective escalation: the ability to recognize when a task-critical gap cannot be resolved from local context and to ask a targeted question at the right time.

> TODO: Insert exact paper numbers/figure reference here, probably from the published SWE-agent results rather than the release-asset reruns.

## Adding Harnesses

We extend the paper by introducing new harnesses per model. We experiment with:


- `GPT-5.5 / SWE-agent`
- `GPT-5.5 / Native Codex`
- `GPT-5.5 / Native Codex Tool`
- `Claude Opus 4.7 / SWE-agent`
- `Claude Opus 4.7 / Native Claude Code`
- `Claude Opus 4.7 / Native Claude Code Tool`
- `Gemini 3.1 Pro / Native ADK`
- `GLM-5P1 / Native OpenCode`


> TODO: Kelvin note maybe we do native codex versus adding the tool as a policy? idk. @alina wdyt. that or remove the policy part. 



## Metrics and Denominators

Unless otherwise noted, `pass@3` is computed over tasks with up to three attempts per system. Result 1 compares FullInfo and AskHuman on the intersected native-harness task set, so each system is evaluated on the same task subset in both modes. AskHuman means the agent must decide whether and when to ask for missing task-critical information; FullInfo means that information is supplied up front. Blocker Recall measures the share of known blockers resolved by the agent's questions, while Ask Precision measures the share of questions that targeted real blockers. Trace-derived strategy and recovery labels are deterministic proxies over logged events, not semantic judge labels.


## Result 1: Stronger Harnesses Still Have A Large Context Drop-Off

Native/current harnesses have high performance with all information supplied (FullInfo) but much lower AskHuman performance on the same tasks. With full information, most agentic systems score at 75-81% pass@3. However, when the same systems must decide whether and when to use AskHuman, pass@3 drops to 13-22%. Our results from HIL-Bench extend beyond just SWE-agent and into other harnesses.

![FullInfo vs AskHuman pass@3](figures/01_same_model_different_scaffold.png)

All four native harnesses sit far below the no-drop-off diagonal, despite high FullInfo pass@3.

- FullInfo pass@3 on the intersection is high: ADK/Gemini `81.0%`, OpenCode/GLM `79.0%`, Codex/GPT-5.5 `76.0%`, Claude Code/Claude Opus `75.0%`.
- AskHuman pass@3 on the same intersected tasks is much lower: ADK/Gemini `20.0%`, OpenCode/GLM `13.0%`, Codex/GPT-5.5 `22.0%`, Claude Code/Claude Opus `15.2%`.


## Result 2: Trustworthiness and Agency

In the original paper, we introduced Ask-F1 to balance models' ability to ask relevant questions without over-asking. We break down the metric into Blocker Recall (how many blockers did the agent resolve) and Ask Precision (how many of the questions it asked were relevant). This gives a sense of how trustworthy an agentic system is (if there's a blocker, can I trust it to clarify) and how agentic it is (can it finish its work without pinging the user indiscriminately). While harness variations can improve Blocker Recall or Ask Precision substantially (Gemini 3.1 Pro on ADK raises both substantially from A/B -> C/D), all agent systems still struggle with Blocker Recall. In these default harness/policy settings, systems are better at targeting blockers once they ask than at deciding that a blocker must be surfaced.




![Detection vs targeting](figures/02_detection_targeting.png)

Ask Precision is often reasonable, but Blocker Recall remains the larger failure mode.

- Several systems have high blocker-targeting precision under the current metadata: Native Codex/GPT-5.5 `71.8%`, Native Codex Tool/GPT-5.5 `67.2%`, Native Claude Code/Claude Opus `65.4%`, Native OpenCode/GLM `63.1%`.
- Blocker Recall is much weaker for many systems: Native Claude Code/Claude Opus `26.7%`, Native OpenCode/GLM `34.5%`, Native Codex/GPT-5.5 `38.0%`.
- Tool/harness variants can move Blocker Recall substantially. GPT-5.5 Native Codex Tool reaches `61.5%` Blocker Recall, versus Native Codex at `38.0%`.


## Result 3: Recovery After A Bad First Ask

We also ask whether an irrelevant first question is recoverable. In real scenarios, we hope that an agent would be able to re-align themselves after a poor question. We argue that a strong system would not simply never ask a bad question, but one that notices the miss and sharpens the question to complete the task.

Using the trace-level AskHuman sequences, we deterministically mark the first irrelevant or incorrect `ask_human()` as `I` and a blocker resolution as `R`. For Codex, we filter out MCP permission prompts such as "Allow the human_input MCP server to run tool `ask_human`?" because those are harness permission events, not actual clarification questions.

Counts and percentages below use first-failed-ask runs as the denominator.

| system | first failed ask runs | solved after first failed ask | asked later relevant question | solved after later relevant question |
|---|---:|---:|---:|---:|
| `GPT-5.5 / SWE-agent` | 135 | 21 / 135 (15.6%) | 96 / 135 (71.1%) | 21 / 135 (15.6%) |
| `GPT-5.5 / Native Codex` | 88 | 4 / 88 (4.5%) | 15 / 88 (17.0%) | 1 / 88 (1.1%) |
| `GPT-5.5 / Native Codex Tool` | 97 | 12 / 97 (12.4%) | 51 / 97 (52.6%) | 10 / 97 (10.3%) |
| `Claude Opus 4.7 / SWE-agent` | 158 | 8 / 158 (5.1%) | 74 / 158 (46.8%) | 7 / 158 (4.4%) |
| `Claude Opus 4.7 / Native Claude Code` | 62 | 4 / 62 (6.5%) | 14 / 62 (22.6%) | 0 / 62 (0.0%) |
| `Claude Opus 4.7 / Native Claude Code Tool` | 51 | 0 / 51 (0.0%) | 10 / 51 (19.6%) | 0 / 51 (0.0%) |
| `Gemini 3.1 Pro / SWE-agent` | 0 | n/a | n/a | n/a |
| `Gemini 3.1 Pro / Native ADK` | 86 | 3 / 86 (3.5%) | 45 / 86 (52.3%) | 3 / 86 (3.5%) |

GPT-5.5 is still the strongest recovery case under SWE-agent, and the Codex Tool variant preserves part of that behavior. Surprisingly, Native Codex alone has high precision when it asks, but much weaker recovery after an initial miss. Claude Code has a few solves after a failed first ask, but none where the solve follows a later relevant clarification in this deterministic trace proxy.


## Result 4: Harnesses Change Strategy, Not Just Scores


This section presents:

1. New trace analysis on the SWE-agent runs shows that similar model families often have similar strategy shapes under the same harness.
2. Once we vary harnesses, the same model family can move to a different asking strategy.

### Result 4a: SWE-Agent Reveals Family-Level Strategy Shapes

The original paper varied models under SWE-agent and reported outcome/ask metrics. We now ask: when the harness is held fixed, do related models behave similarly?

The answer seems to be yes. Model families seem to have similar strategy shapes under SWE-agent. The GPT family actually asks earlier than every other model class -- they ask for clarification immediately. Claude models explore before asking. Even still, we know that model preferences do not translate to success. GPT pass@3 varies between (TODO: X and Y), despite sharing the same general plan.


![SWE-agent model-family strategy](figures/13_swe_agent_model_family_strategy.svg)

### Result 4b: Asking Strategy Is Also Affected by Harness

While models within the same family had similar strategies, the tendency is not invariant to harness choice. Under Codex, which discourages asking in the system prompt, GPT's preference to ask early disappears. Claude under Claude Code has a lot more thinking turns than before. Even for less opinionated harnesses, like ADK, we see far less testing.

![GPT action phenotypes](figures/06_action_phenotypes_gpt.png)

![Claude action phenotypes](figures/06_action_phenotypes_claude.png)

![Gemini action phenotypes](figures/06_action_phenotypes_gemini.png)





### Result 4c: Timing and Strategy Vary

We bin the generic strategies to make the harness effect more visible. SWE-agent often pushes asking earlier; Native Codex tends to explore before asking; the Codex Tool variant shifts further toward explore-then-ask while also improving Blocker Recall. They are different collaboration policies induced by the model-harness system.


![Strategy buckets](figures/05_strategy_buckets.png)


Likewise, the timing of the asks change. Some models like Claude Opus 4.7 will ask later on Claude Code as opposed to SWE-agent.

![First ask timing](figures/08_first_ask_timing.png)



- `GPT-5.5 / Native Codex Tool`: `84.8%` explored then asked before writing; `11.9%` asked upfront before reading; `3.3%` never asked.
- `GPT-5.5 / SWE-agent`: `70.4%` asked upfront before reading; `29.6%` explored then asked before writing.
- `GPT-5.5 / Native Codex`: `70.9%` explored then asked before writing; `17.0%` asked upfront; `11.2%` never asked.
- `GPT-5.4 / SWE-agent`: `98.9%` asked upfront before reading while pass@3 was only `1.3%`, a useful reminder that "asking early" is not the same as "asking well."
- `Claude Opus 4.7 / Native Claude Code`: nearly half explored then asked, but `43.5%` never asked.
- `GLM-5P1 / Native OpenCode`: roughly split between explored-then-asked and no-ask, with the OpenCode parser/harness caveat below.


## Result 5: Terminal States Show Different Failure Anatomy

Failed AskHuman trajectories end in different deterministic terminal states. This is useful for diagnosing how systems fail after, before, or around the help-seeking step. Again, we find that failures vary not only by the model, but also by the harness itself.


![Terminal evidence mix](figures/04_terminal_evidence_mix.png)


## Result 6: HIL-Bench As Harness-Design Feedback

The constructive punchline should be that HIL-Bench is useful not only as a model benchmark, but as feedback for how we build engineering agents. In real engineering workflows, we do not rely on the base model alone: we add project-specific skills, tools, conventions, and escalation policies. If prompted-skill / HIL-tuned harness runs improve pass@3, Blocker Recall, or Ask-F1, we should report them explicitly as harness-level interventions rather than silently merging them into the native baseline.


**Intended figure:** TODO, probably a compact panel comparing:

- FullInfo / no-drop-off ceiling
- original paper SWE-agent setting
- native/current AskHuman
- HIL-tuned skill harness
- custom ask tool, if useful

**Waiting on results:** coauthor-provided prompted-skill / custom-tool numbers.
