# Remote critical-log sender

This script is intended to run on the server that already hosts NemoClaw and
`redline-watcher-6`. ClawWatch does not connect to the remote server.

## Automatically send logs from replay

Run `log_backend` on the NemoClaw server. **Auto-send critical logs** is enabled by
default at the top of the Gradio dashboard. The adjacent status shows total delivered,
pending/retry counts, and the latest pending error.
Turning it off waits for any current send and retains pending logs; turning it back
on resumes delivery. The control affects all users of this server and resets to the
startup flag on restart.

Normal startup enables automatic delivery:

```bash
cd log_backend
./scripts/start_gradio.sh
```

Equivalent CLI: `.venv/bin/clawwatch-demo serve --config config/demo.toml`.
Start a replay in the dashboard. Every newly emitted critical event is automatically
queued and sent through the bundled `scripts/remote/send_critical_log.py`; no manual
pipe or review card is required. The complete original JSON is passed through stdin.
Use `--no-auto-send-critical` to disable delivery at startup, for example when running
locally. `--auto-send-critical` remains supported as an explicit enable flag.

The server checks the queue every second, including while the browser is closed.
Sending runs separately from replay and never holds a database write transaction.
Failures remain queued and become eligible for retry after 30 seconds. Pending
records resume when delivery is re-enabled, including at normal startup.
Successful deliveries are
persisted and skipped on later checks. The dashboard displays pending/retry counts,
and History records `critical_log_sent` and `critical_log_failed` events.

Only critical events emitted while automatic delivery is enabled enter the queue; imported
source records and old replay history are not backfilled. Replaying the same source
again creates a new event and a new alert. The existing review-card Slack notifier
is separate and may also send an alert if you add a critical event to the review board.

The defaults below apply to automatic delivery too. Set `NEMOCLAW_BIN` to an absolute
executable path before starting the server if needed. The sender also checks
`~/.local/bin/nemoclaw` when the command is not on PATH, and includes `~/.local/bin`
on the child process PATH. Recognized failures are reported with safe diagnostic
messages; command output and log payloads are not printed. No Slack bot token is required
by this delivery path; the existing NemoClaw/OpenClaw Slack configuration is used.
Shutdown waits for the current send to finish (up to its timeout). A crash or timeout
after Slack accepts a message but before delivery is recorded can cause a duplicate
on retry; delivery is not exactly-once.

## Install it on the remote server

For a different log generator, the standalone script can still be installed separately.

Copy [`scripts/remote/send_critical_log.py`](../scripts/remote/send_critical_log.py)
to the remote host and make it executable:

```bash
install -m 0755 send_critical_log.py "$HOME/.local/bin/send-critical-log"
```

The defaults match your current setup:

- NemoClaw gateway port: `8991`
- Sandbox: `redline-watcher-6`
- Slack target: `channel:cyber-alerts`

If `nemoclaw` is not on the noninteractive `PATH`, set its absolute path when
calling the script, for example `--nemoclaw /home/dell/.local/bin/nemoclaw`.

## Call it when a log is generated

Pipe the complete JSON log into the script. It reads `severity`, `level`,
`log.severity`, or `log.level`, ignores noncritical records, and sends the entire
critical JSON text without field selection, normalization, or truncation:

```bash
printf '%s\n' '{"severity":"critical","message":"example","details":{"id":42}}' \
  | "$HOME/.local/bin/send-critical-log"
```

For a producer that already knows the severity but emits JSON without a severity
field, provide the override:

```bash
generate_complete_json_log \
  | "$HOME/.local/bin/send-critical-log" --severity critical
```

To process a JSONL file or stream one record per line:

```bash
"$HOME/.local/bin/send-critical-log" --jsonl --file /path/to/events.jsonl
```

Validate formatting and filtering without calling NemoClaw:

```bash
printf '%s\n' '{"severity":"critical","message":"test"}' \
  | "$HOME/.local/bin/send-critical-log" --dry-run
```

The script invokes the equivalent of:

```bash
export NEMOCLAW_GATEWAY_PORT=8991
nemoclaw redline-watcher-6 exec -- openclaw message send \
  --channel slack --target channel:cyber-alerts --message "<complete JSON log>"
```

It passes arguments directly to the process without a shell, so quotes,
newlines, backticks, and `$()` inside log data cannot execute commands. A failure
returns a nonzero exit status without printing the remote command output. The
caller should retry failed records and preserve them until the command succeeds.
