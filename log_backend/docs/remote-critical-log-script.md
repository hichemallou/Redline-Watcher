# Remote critical-log sender

This script is intended to run on the server that already hosts NemoClaw and
`redline-watcher-3`. ClawWatch does not connect to the remote server.

## Install it on the remote server

Copy [`scripts/remote/send_critical_log.py`](../scripts/remote/send_critical_log.py)
to the remote host and make it executable:

```bash
install -m 0755 send_critical_log.py "$HOME/.local/bin/send-critical-log"
```

The defaults match your current setup:

- NemoClaw gateway port: `8991`
- Sandbox: `redline-watcher-3`
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
nemoclaw redline-watcher-3 exec -- openclaw message send \
  --channel slack --target channel:cyber-alerts --message "<complete JSON log>"
```

It passes arguments directly to the process without a shell, so quotes,
newlines, backticks, and `$()` inside log data cannot execute commands. A failure
returns a nonzero exit status without printing the remote command output. The
caller should retry failed records and preserve them until the command succeeds.
