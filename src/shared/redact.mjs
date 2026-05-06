const SECRET_KEY_PATTERN = /(?:ANTHROPIC_AUTH_TOKEN|LITELLM.*KEY|CODEX_API_KEY|OPENAI_API_KEY|AWS.*SECRET|.*TOKEN|.*API_KEY)/i;
const SECRET_ASSIGNMENT_PATTERN =
  /((?:ANTHROPIC_AUTH_TOKEN|LITELLM[_A-Z0-9]*KEY|LITELLM_PROXY_API_KEY|CODEX_API_KEY|OPENAI_API_KEY|AWS[_A-Z0-9]*SECRET|[A-Z0-9_]*TOKEN|[A-Z0-9_]*API_KEY)=)([^\s"'`\\]+)/gi;
const OPENAI_STYLE_SECRET_PATTERN = /(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}/g;

export function redactString(text = "") {
  let redacted = String(text);
  for (const [key, value] of Object.entries(process.env)) {
    if (!value || value.length < 8) continue;
    if (!SECRET_KEY_PATTERN.test(key)) continue;
    redacted = redacted.split(value).join("[REDACTED]");
  }
  redacted = redacted.replace(SECRET_ASSIGNMENT_PATTERN, "$1[REDACTED]");
  redacted = redacted.replace(OPENAI_STYLE_SECRET_PATTERN, "[REDACTED]");
  return redacted;
}
