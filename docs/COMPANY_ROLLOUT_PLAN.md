# Company Rollout Plan

This repository is safe to share inside the company as a reusable single-user automation template. Each person should deploy their own Railway service with their own TodoMate and Slack credentials.

## What we learned from production operation

1. Do not disable the scheduler when a daily report fails.
   - A missed send is usually caused by expired TodoMate/PlayMCP or Slack credentials, not by the cron schedule itself.
   - Keep Railway cron enabled and treat auth failures as health-check alerts.
2. Slack credentials can expire or become unreadable by automatic extraction.
   - Always verify with `agent-slack auth status --pretty`.
   - If it becomes invalid, renew credentials locally and update Railway variables.
3. PlayMCP/TodoMate credentials can require re-authentication.
   - The Hermes health check records credential status and sends a best-effort Slack warning when renewal is likely needed.
4. One Railway service should belong to one user.
   - Do not share one set of TodoMate/Slack credentials across people.
   - For multiple company users, duplicate the Railway service/project per person or build the multi-user roadmap from README.

## Recommended company setup

For each user:

1. Fork or clone this repository.
2. Authenticate TodoMate locally with `mcporter`.
3. Authenticate Slack locally with `agent-slack`.
4. Choose the Slack DM/channel ID for reports.
5. Run local dry-run.
6. Create a Railway project/service and mount a `/data` volume.
7. Set that user's own Railway variables.
8. Deploy and keep the cron enabled.
9. Check `diagnose` and Railway logs after the first scheduled run.

## Renewal checklist

Run locally:

```bash
agent-slack auth status --pretty
mcporter call mcp-gateway.TodoMate-loadGoals
```

If Slack is invalid:

1. Renew `~/.config/agent-messenger/slack-credentials.json` locally.
2. Re-check `agent-slack auth status --pretty`.
3. Base64 encode the renewed credentials.
4. Update `AGENT_MESSENGER_SLACK_CREDENTIALS_JSON_B64` in Railway.
5. Run a forced manual send or `diagnose`.

If TodoMate/PlayMCP is invalid:

1. Re-authenticate with `mcporter auth mcp-gateway` or the current PlayMCP one-time-token flow.
2. Re-check `mcporter call mcp-gateway.TodoMate-loadGoals`.
3. Base64 encode the renewed `~/.mcporter/credentials.json`.
4. Update `MCPORTER_CREDENTIALS_JSON_B64` in Railway.
5. Run a forced manual send or `diagnose`.

## Operating rule

Do not turn off Railway cron to hide failures. Keep the scheduler active, renew credentials, and use the health-check output to decide whether Slack or TodoMate auth needs attention.
