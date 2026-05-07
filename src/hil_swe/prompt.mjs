/**
 * SWE task prompt construction for trust_horizon.
 *
 * ask_human mode: standard SWE-bench instance template + clarification instruction
 * full_info  mode: standard SWE-bench instance template + blocker resolutions
 *                  (matches hil_bench/templates/problem_full_info.jinja2)
 *
 * The instance template format matches hil_bench/configs/swe/ask_config_claude_opus_4-6.yaml
 * instance_template so trajectories are comparable to the public benchmark runs.
 */

const WORKSPACE = "/testbed";

/**
 * Standard SWE-bench instance template preamble (matches hil-bench configs exactly).
 * The {{problem_statement}} placeholder is filled with the actual problem.
 */
function instanceTemplate(problemStatement) {
  return [
    `<uploaded_files>`,
    WORKSPACE,
    `</uploaded_files>`,
    `I've uploaded a code repository in the directory ${WORKSPACE}. Consider the following PR description:`,
    ``,
    `<pr_description>`,
    problemStatement,
    `</pr_description>`,
    ``,
    `Can you help me implement the necessary changes to the repository so that the requirements specified in the <pr_description> are met?`,
    `I've already taken care of all changes to any of the test files described in the <pr_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!`,
    `Your task is to make the minimal changes to non-tests files in the ${WORKSPACE} directory to ensure the <pr_description> is satisfied. **Do not make changes for anything that isn't explicitly or implicitly requested in the <pr_description>.**`,
    ``,
    `Explore the codebase only as much as you need to understand the problem; prioritize actually implementing and testing your changes. Think about edge cases and make sure your changes handle them as well!`,
    ``,
    `If you encounter trouble using any tool, find alternative ways to achieve the same goal, e.g. different tool arguments, a set of different tools, etc.`,
  ].join("\n");
}

/**
 * Build the agent-facing prompt for a SWE task.
 *
 * @param {object} opts
 * @param {string} opts.problemStatement  - raw problem statement from problem_statement.txt
 * @param {"ask_human"|"full_info"} opts.mode
 * @param {Array<{description: string, resolution: string}>} [opts.blockers]
 *   - only needed for full_info mode; ignored for ask_human
 * @returns {string}
 */
export function buildSwePrompt({ problemStatement, mode, blockers = [] }) {
  if (mode === "full_info") return buildFullInfoPrompt(problemStatement, blockers);
  if (mode === "ask_human") return buildAskHumanPrompt(problemStatement);
  throw new Error(`Unknown SWE prompt mode: ${mode}. Expected "ask_human" or "full_info".`);
}

/**
 * full_info prompt — instance template wrapping problem statement, followed by the
 * Additional Context section from hil_bench/templates/problem_full_info.jinja2:
 *
 *   {{ problem_statement }}      (embedded in instance_template)
 *
 *   ---
 *
 *   ## Additional Context
 *
 *   The following clarifications are provided to help you complete this task:
 *
 *   {% for blocker in blockers %}
 *   ### {{ blocker["description"] }}
 *
 *   {{ blocker["resolution"] }}
 *   {% endfor %}
 */
function buildFullInfoPrompt(problemStatement, blockers) {
  const base = instanceTemplate(problemStatement);
  if (!blockers.length) return base;
  const sections = blockers
    .map((b) => `### ${b.description}\n\n${b.resolution}`)
    .join("\n\n");
  return [
    base,
    "",
    "---",
    "",
    "## Additional Context",
    "",
    "The following clarifications are provided to help you complete this task:",
    "",
    sections,
    "",
  ].join("\n");
}

/**
 * ask_human prompt — instance template (no blocker info) so the agent has to ask.
 * The system prompt (configured in run_claude.mjs) provides the ask_human instructions.
 */
function buildAskHumanPrompt(problemStatement) {
  return instanceTemplate(problemStatement);
}
