/**
 * SWE task prompt construction for trust_horizon.
 *
 * ask_human mode: standard SWE-bench instance template
 * full_info mode: standard SWE-bench instance template + blocker resolutions
 *                  (matches hil_bench/templates/problem_full_info.jinja2)
 *
 * The instance template format matches hil_bench/configs/swe/ask_config_claude_opus_4-6.yaml
 * instance_template so trajectories are comparable to the public benchmark runs.
 */

const WORKSPACE = "/app";

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
 *   - only needed for full_info mode; ignored otherwise
 * @returns {string}
 */
export function buildSwePrompt({ problemStatement, mode, blockers = [] }) {
  if (mode === "full_info") return buildFullInfoPrompt(problemStatement, blockers);
  if (mode === "ask_human") return buildBasePrompt(problemStatement);
  throw new Error(`Unknown SWE prompt mode: ${mode}. Expected ask_human or full_info.`);
}

/**
 * full_info prompt — mirrors the canonical hil_bench "with_blockers" flow exactly:
 *
 *   STEP 1 — hil_bench/templates/problem_with_blockers.jinja2 augments the raw
 *             problem_statement with the Additional Context section.
 *   STEP 2 — The augmented text is passed as {{problem_statement}} to the
 *             SWE-agent instance_template, so it lands INSIDE <pr_description>.
 *
 * CRITICAL: the "---\n## Additional Context" section must be INSIDE <pr_description>,
 * not appended after it.  The old implementation called instanceTemplate() first and
 * then appended the blockers section after </pr_description>, which was wrong.
 */
function buildFullInfoPrompt(problemStatement, blockers) {
  if (!blockers.length) return instanceTemplate(problemStatement);
  // Step 1: augment problem_statement (mirrors problem_with_blockers.jinja2).
  const sections = blockers
    .map((b) => `### ${b.description}\n\n${b.resolution}`)
    .join("\n\n");
  const augmentedProblem = [
    problemStatement,
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
  // Step 2: wrap augmented text in the SWE-agent instance template.
  return instanceTemplate(augmentedProblem);
}

/**
 * Base prompt — instance template without blocker info or extra ask guidance.
 */
function buildBasePrompt(problemStatement) {
  return instanceTemplate(problemStatement);
}
