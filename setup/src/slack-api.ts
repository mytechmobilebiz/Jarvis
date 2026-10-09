import https from "node:https";

const SLACK_API_TIMEOUT_MS = 10_000;

export interface SlackResponse {
  ok: boolean;
  error?: string;
  needed?: string;
  /** Granted token scopes from Slack's x-oauth-scopes response header. */
  scopes?: string[];
  /** Workspace name, as auth.test reports it. */
  team?: string;
  /** auth.test with an app-level token answers with the app's own ID. */
  app_id?: string;
  /** auth.test with a bot token answers with the bot's ID, not its app's. */
  bot_id?: string;
  /** bots.info: the app a bot belongs to. */
  bot?: {
    app_id?: string;
  };
  channel?: {
    id?: string;
    name?: string;
  };
  channels?: Array<{
    id: string;
    name: string;
  }>;
  response_metadata?: {
    next_cursor?: string;
  };
}

export async function slackApi(
  method: string,
  token: string,
  params: Record<string, string> = {},
): Promise<SlackResponse> {
  const body = new URLSearchParams(params).toString();

  return new Promise<SlackResponse>((resolve) => {
    const request = https.request(
      {
        hostname: "slack.com",
        path: `/api/${method}`,
        method: "POST",
        timeout: SLACK_API_TIMEOUT_MS,
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/x-www-form-urlencoded",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (response) => {
        let data = "";
        response.on("data", (chunk: Buffer) => {
          data += chunk.toString("utf8");
        });
        response.on("end", () => {
          try {
            const result = JSON.parse(data) as SlackResponse;
            const scopes = response.headers["x-oauth-scopes"];
            if (scopes !== undefined) result.scopes = String(scopes).split(",").map((scope) => scope.trim()).filter(Boolean);
            resolve(result);
          } catch {
            resolve({ ok: false, error: `http_${response.statusCode ?? "unknown"}` });
          }
        });
      },
    );

    request.on("error", (error) => {
      resolve({ ok: false, error: error.message });
    });
    request.on("timeout", () => {
      request.destroy(new Error(`Slack API request ${method} timed out after 10 seconds.`));
    });
    request.write(body);
    request.end();
  });
}

export async function listSlackChannels(token: string): Promise<{ ids: Record<string, string>; error?: string; needed?: string }> {
  const result: Record<string, string> = {};
  let cursor = "";
  do {
    const response = await slackApi("conversations.list", token, {
      exclude_archived: "true",
      limit: "1000",
      types: "public_channel",
      ...(cursor === "" ? {} : { cursor }),
    });
    if (!response.ok) return { ids: result, error: response.error, needed: response.needed };
    for (const channel of response.channels ?? []) {
      result[channel.name] = channel.id;
    }
    cursor = response.response_metadata?.next_cursor ?? "";
  } while (cursor !== "");
  return { ids: result };
}
