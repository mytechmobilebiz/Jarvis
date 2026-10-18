// Prints the credentials the Command Center workflows reference, as n8n's
// import:credentials JSON. Values come from the environment, never from git.
const credentials = [
  {
    id: "ccApiToken000001",
    name: "Command Center API token",
    type: "httpHeaderAuth",
    data: { name: "Authorization", value: `Bearer ${process.env.CC_SERVICE_TOKEN}` },
  },
  {
    id: "ccWebhookSecret1",
    name: "Command Center webhook secret",
    type: "httpHeaderAuth",
    data: { name: "X-CC-Webhook-Secret", value: process.env.N8N_WEBHOOK_SECRET },
  },
];
process.stdout.write(JSON.stringify(credentials));
