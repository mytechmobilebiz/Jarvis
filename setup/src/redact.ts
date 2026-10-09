const TOKEN_PATTERN = /\b(?:sk|xoxb|xoxp|xapp)-[A-Za-z0-9._=-]+/g;

export function redactSecrets(value: string): string {
  return value.replace(TOKEN_PATTERN, "[redacted-token]");
}
