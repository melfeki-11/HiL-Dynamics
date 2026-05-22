# HIL-Bench Model-Harness Narrative Working Draft

This is a rough coauthor-facing draft, not final prose. The goal is to capture the argument we are converging on quickly enough that others can revise, correct labels, and decide which figures to keep. Please remove this once all of us agree to this narrative.

The original HIL-Bench paper established a selective-escalation failure mode under a controlled evaluation harness: agents can solve many tasks when missing information is supplied up front, but recover only a fraction of that performance when they must decide whether and when to ask for help.

However, modern native harnesses such as Claude Code and Codex are much stronger than the SWE-agent setup we used before. Within each harness, tool surfaces and skill guidance can also vary: some encourage asking, some discourage it, and some add task-specific instructions. We therefore follow up on HIL-Bench with a different question: do models still fail with stronger harnesses and skills?

We evaluate agents as a coupled `<model, harness, skills>` system, not the model alone. Stronger native harnesses and custom skills variants do change behavior. They change how often agents ask and when they ask. These variations, however, do not make the underlying selective-escalation gap completely vanish.

1. HIL-Bench remains hard even for what the community thinks are stronger harnesses.
2. Harness and skill design visibly changes the failure mode.
3. Steering agent behavior with prompting skills, a common agent-engineering practice, helps agentic systems on HIL-Bench.

## Paper Context: What HIL-Bench Already Showed

The original paper used SWE-agent for clean experimentation. HIL-Bench showed that many SWE tasks are solvable when missing information is supplied up front, but are much harder when agents must decide whether to seek human help. The core failure is selective escalation: agents often miss task-critical blockers or ask vague, mistimed questions with an underspecified task.

> HIL-Bench measures selective escalation: the ability to recognize when a task-critical gap cannot be resolved from local context and to ask a targeted question at the right time.

> TODO: Insert exact paper numbers/figure reference here, probably from the published SWE-agent results rather than the release-asset reruns.

## Adding Harnesses

We extend the paper by introducing new harnesses per model. We include the following from our original benchmark:

- `GPT-5.5 / SWE-agent / ask_human() tool`
- `Claude Opus 4.7 / SWE-agent / ask_human() tool`
- `Gemini 3.1 Pro / SWE-agent / ask_human() tool`

In addition, we add native harnesses for these models. The following harnesses have built-in asking:

- `GPT-5.5 / Native Codex / default`
- `Claude Opus 4.7 / Native Claude Code / default`

The native harnesses for Claude and GPT, however, strongly bias against asking questions. To counter that, we also provide tool guidance that strongly suggests using a custom AskHuman tool. We include equivalent tools for OpenCode and ADK, which do not have built-in ask tools.

- `Gemini 3.1 Pro / Native ADK / custom ask_human() tool`
- `GLM-5P1 / Native OpenCode / custom ask_human() tool`
- `Claude Opus 4.7 / Native Claude Code / custom ask_human() tool`
- `GPT-5.5 / Native Codex / custom ask_human() tool`

Finally, we test whether custom skills, a common agent-engineering practice, can steer agents toward better collaboration. The skill in `skills.md` encourages agents to explore before editing and ask when local context cannot resolve a blocker.

- `Claude Opus 4.7 / Native Claude Code / with custom skills.md`
- `GPT-5.5 / Native Codex / with custom skills.md`

## Result 1: Stronger Harnesses Still Have A Large Context Drop-Off

Native/current harnesses have high performance with all information supplied (FullInfo) but much lower AskHuman performance on the same tasks. With full information, most agentic systems score at 77-80% pass@3. However, when forcing the same systems to decide when and how to ask for clarifications, pass@3 drops to 12-21%. Our results from HIL-Bench extend beyond just SWE-agent and into other harnesses.

The custom-skill runs should be read as a harness intervention rather than a direct FullInfo comparison. In the figure, they are plotted against the matching base native FullInfo ceiling. On the clean 150-task custom-skill rerun, `GPT-5.5 / Native Codex Tool + custom skill` reaches `51.3%` pass@3, much closer to the FullInfo ceiling than the baseline AskHuman systems, while `Claude Opus 4.7 / Native Claude Code Tool + custom skill` remains low at `14.0%`. This is the first hint that the intervention helps one model-harness pair much more than another.

![FullInfo vs AskHuman pass@3](figures/01_same_model_different_scaffold.png)

- FullInfo pass@3 is high: ADK/Gemini `80.0%`, OpenCode/GLM `79.3%`, Codex/GPT-5.5 `78.0%`, Claude Code/Claude Opus `76.7%`.
- AskHuman pass@3 on the same intersected tasks is much lower: ADK/Gemini `21.3%`, OpenCode/GLM `11.7%`, Codex/GPT-5.5 `20.3%`, Claude Code/Claude Opus `13.2%`.
- Custom-skill rows are also below the matched base FullInfo ceiling: Codex/GPT-5.5 custom skill `51.3%` vs Codex FullInfo `78.0%`; Claude Code/Claude Opus custom skill `14.0%` vs Claude Code FullInfo `76.7%`.


## Result 2: Trustworthiness and Agency

In the original paper, we introduced Ask-F1 to balance models' ability to ask relevant questions without over-asking. We break down the metric into Blocker Recall (how many blockers the agent resolved) and Ask Precision (how many of the questions it asked were relevant). This gives a sense of how trustworthy an agentic system is (if there is a blocker, can I trust it to clarify?) and how agentic it is (can it finish without pinging the user indiscriminately?). Harness variations can improve recall or precision substantially, but all agentic systems still struggle with Blocker Recall. Today's systems are not built to surface blockers. When the agents do ask, however, they do so with reasonable precision.

The custom-skill intervention makes the harness-intervention point more concrete. `GPT-5.5 / Native Codex Tool + custom skill` has lower Ask Precision than the earlier Codex Tool run (`48.9%` vs `64.9%` in the current combined dry run), but higher Blocker Recall (`67.0%` vs `61.2%`) and higher pass@3 (`51.3%` vs `42.0%`). It seems to spend more question budget to recover more blockers. The Claude custom-skill row does not show the same payoff: `43.6%` precision, `31.3%` recall, and `14.0%` pass@3 on the clean 150-task rerun.

![Detection vs targeting](figures/02_detection_targeting.png)


- Several systems have high blocker-targeting precision under the current metadata: Native Codex/GPT-5.5 `71.8%`, Native Codex Tool/GPT-5.5 `67.2%`, Native Claude Code/Claude Opus `65.4%`, Native OpenCode/GLM `63.1%`.
- Blocker recall is much weaker for many systems: Native Claude Code/Claude Opus `26.7%`, Native OpenCode/GLM `34.5%`, Native Codex/GPT-5.5 `38.0%`.
- Tool/harness variants can move recall substantially. GPT-5.5 Native Codex Tool reaches `61.5%` recall, versus Native Codex at `38.0%`.
- The Codex custom-skill intervention pushes this further in the filtered run: `67.0%` recall and `51.3%` pass@3, at the cost of more questions (`4.87` per pass) and lower precision (`48.9%`).


## Result 3: Recovery After A Bad First Ask

We also ask whether an irrelevant first question is recoverable. In real scenarios, we hope that an agent would be able to re-align themselves after a poor question. We argue that a strong system would not simply never ask a bad question, but one that notices the miss and sharpens the question to complete the task.

Using the trace-level ask sequences, we deterministically mark the first irrelevant or incorrect `ask_human()` as `I` and a blocker resolution as `R`. For Codex, we filter out MCP permission prompts such as "Allow the human_input MCP server to run tool `ask_human`?" because those are harness permission events, not actual clarification questions.

Percentages below use first-failed-ask runs as the denominator. The table is regenerated from `data/bad_first_ask_recovery.csv`. For Gemini 3.1 Pro / SWE-agent, raw trajectories were not available in this release bundle, so the recovery columns are inferred from the analysis CSV's first-ask-irrelevant flag and aggregate relevant-question count rather than an ordered ask sequence.

| system | first failed ask runs | solved after first failed ask | asked later relevant question | solved after later relevant question |
|---|---:|---:|---:|---:|
| `GPT-5.5 / SWE-agent` | 135 | 15.6% | 71.1% | 15.6% |
| `GPT-5.5 / Native Codex` | 67 | 6.0% | 16.4% | 1.5% |
| `GPT-5.5 / Native Codex Tool` | 107 | 19.6% | 57.0% | 17.8% |
| `GPT-5.5 / Native Codex Tool + custom skill` | 118 | 30.5% | 72.9% | 28.8% |
| `Claude Opus 4.7 / SWE-agent` | 158 | 5.1% | 46.8% | 4.4% |
| `Claude Opus 4.7 / Native Claude Code` | 63 | 6.3% | 20.6% | 0.0% |
| `Claude Opus 4.7 / Native Claude Code Tool` | 59 | 0.0% | 18.6% | 0.0% |
| `Claude Opus 4.7 / Native Claude Code Tool + custom skill` | 86 | 1.2% | 37.2% | 1.2% |
| `Gemini 3.1 Pro / SWE-agent` | 112 | 0.9% | 83.0% | 0.9% |
| `Gemini 3.1 Pro / Native ADK` | 102 | 5.9% | 56.9% | 5.9% |

GPT-5.5 is still the strongest recovery case under SWE-agent, and the Codex Tool variant preserves part of that behavior. The Codex custom-skill intervention is the cleanest positive example here: after a bad first ask, it more often asks a later relevant question and more often solves after doing so. Claude Code's custom-skill run moves the later-relevant-question rate up, but that does not yet translate into solves in this deterministic trace proxy.

## Result 4: Harnesses Change Strategy, Not Just Scores

This section presents:

1. New trace analysis on the SWE-agent runs shows that similar model families often have similar strategy shapes under the same harness.
2. Once we vary harnesses, the same model family can move to a different asking strategy.

### Result 4a: SWE-Agent Reveals Family-Level Strategy Shapes

The original paper varied models under SWE-agent and reported outcome/ask metrics. We now ask: when the harness is held fixed, do related models behave similarly?

The answer seems to be yes. Model families seem to have similar strategy shapes under SWE-agent. GPT models ask early, while Claude models do more early exploration before asking. This raw-count fingerprint is not normalized within each turn, so it keeps the same action-volume feel as the strategy-bucket plots below.


![SWE-agent model-family strategy](figures/13_swe_agent_model_family_strategy.svg)

### Result 4b: Asking Strategy Is Also Affected by Harness

While models within the same family had similar strategies, the tendency is not invariant to harness choice. Under Codex, which discourages asking in the system prompt, GPT's preference to ask early disappears. Native Codex shifts GPT-5.5 toward more early exploration, and the tuned custom-skill setup preserves that exploratory workflow while asking more overall.

To make the comparison readable, we collapse each trajectory into a coarse strategy bucket based on whether the agent asks immediately, explores before asking, writes before asking, or never asks. Codex + custom skill becomes almost completely explore-then-ask-before-write: `99.8%` of pass rows ask before editing, with median first ask at turn `18` and median first write at turn `36`.

![Codex strategy buckets](figures/14_codex_strategy_buckets.png)

### Result 4c: Timing and Strategy Vary

We bin the generic strategies to make the harness effect more visible. SWE-agent often pushes asking earlier; Native Codex tends to explore before asking; the Codex Tool variant shifts further toward explore-then-ask while also improving recall. They are different collaboration styles induced by the model-harness system.


![Strategy buckets](figures/05_strategy_buckets.png)


Likewise, the timing of the asks changes. Some models, such as Claude Opus 4.7, ask later on Claude Code than on SWE-agent.

![First ask timing](figures/08_first_ask_timing.png)



- `GPT-5.5 / Native Codex Tool`: `84.8%` explored then asked before writing; `11.9%` asked upfront before reading; `3.3%` never asked.
- `GPT-5.5 / Native Codex Tool + custom skill`: `100.0%` explored then asked before writing in the current strategy bucket CSV; in the timing CSV this appears as `99.8%` ask-before-write plus one ask-without-write row.
- `Claude Opus 4.7 / Native Claude Code Tool + custom skill`: `59.6%` explored then asked before writing, `10.2%` wrote before the first ask, and `29.8%` never asked.
- `GPT-5.5 / SWE-agent`: `70.4%` asked upfront before reading; `29.6%` explored then asked before writing.
- `GPT-5.5 / Native Codex`: `70.9%` explored then asked before writing; `17.0%` asked upfront; `11.2%` never asked.
- `Claude Opus 4.7 / Native Claude Code`: nearly half explored then asked, but `43.5%` never asked.
- `GLM-5P1 / Native OpenCode`: roughly split between explored-then-asked and no-ask, with the OpenCode parser/harness caveat below.


## Result 5: Terminal States Show Different Failure Anatomy

Failed AskHuman trajectories end in different deterministic terminal states. This is useful for diagnosing how systems fail after, before, or around the help-seeking step. Again, we find that failures vary not only by the model, but also by the harness itself.

The custom-skill rows help interpret the successful Codex shift. Among unresolved `GPT-5.5 / Native Codex Tool + custom skill` passes, `35.5%` end as patch-made/no-submit, `32.4%` as local-green/hidden-red, `16.9%` as visible-red-at-end, and `15.2%` as weak-validation-only. This looks less like pure refusal to engage and more like agents doing substantial work but failing to close the loop. Claude custom skill has a similar unresolved profile, with `37.9%` local-green/hidden-red and `35.7%` patch-made/no-submit.


![Terminal evidence mix](figures/04_terminal_evidence_mix.png)


## Harness Intervention Feedback

The constructive punchline is that HIL-Bench is useful not only as a model benchmark, but as feedback for how we build engineering agents. In real engineering workflows, we do not rely on the base model alone: we add project-specific skills, tools, conventions, and escalation guidance. Given some of the errors above, the natural intervention is to teach agents to slow down, explore before editing, and use AskHuman when a blocker cannot be resolved from local context.

The custom-skill runs are our first concrete harness intervention. They use the four example9 shards (`skill_smoke10`, `smoke40`, `smoke50b`, and `smoke50c`). The latest regeneration has all three passes cleanly evaluated for all 150 custom-skill tasks for both Codex and Claude. The only remaining current infra errors in the ingested native runs are four OpenCode baseline pass rows.

| system | clean tasks | pass@1 | pass@3 | Ask Precision | Blocker Recall | Ask-F1 | avg questions |
|---|---:|---:|---:|---:|---:|---:|---:|
| `Claude Opus 4.7 / Native Claude Code Tool + custom skill` | `150` | `10.0%` | `14.0%` | `43.6%` | `31.3%` | `36.5%` | `2.55` |
| `GPT-5.5 / Native Codex Tool + custom skill` | `150` | `36.7%` | `51.3%` | `48.9%` | `67.0%` | `56.5%` | `4.87` |

If these numbers survive final validation, they support the main interpretation: the selective-escalation gap is not just a model property. It is also shaped by the harness and skill layer, and targeted engineering guidance can move both task success and Blocker Recall. In the current combined run, the older `GPT-5.5 / Native Codex Tool` group is at `42.0%` pass@3 and `61.2%` blocker recall, while the Codex custom-skill group is at `51.3%` pass@3 and `67.0%` blocker recall.

**Waiting on:** final denominator decision and confirmation that the custom-skill rows should be compared directly against the native baselines.
