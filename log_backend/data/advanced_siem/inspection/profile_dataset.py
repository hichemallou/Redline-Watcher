"""Read-only corpus audit; does not implement the ClawWatch agent."""
import collections
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / 'raw/advanced_siem_dataset.jsonl'
expected = 'b30902649de9197376e7b245045c6f6721ba3039edc1b7cac3474cc2ad0c4f21'
digest = hashlib.sha256(path.read_bytes()).hexdigest()
assert digest == expected, 'Download checksum mismatch'
rows = [json.loads(line) for line in path.open()]
counts = lambda values: dict(collections.Counter(values))
present = collections.Counter(k for r in rows for k, v in r.items() if v is not None)
auth = [r for r in rows if r['event_type'] == 'auth']
times = [datetime.datetime.fromisoformat(r['timestamp']) for r in rows]
def failed_window(key):
    groups = collections.defaultdict(list)
    for r in auth:
        if r['action'] == 'failed':
            groups[tuple(r.get(k) for k in key)].append(datetime.datetime.fromisoformat(r['timestamp']))
    maximum = 0
    for events in groups.values():
        events.sort()
        left = 0
        for right, event in enumerate(events):
            while (event - events[left]).total_seconds() > 300:
                left += 1
            maximum = max(maximum, right - left + 1)
    return maximum
profile = {
    'rows': len(rows), 'bytes': path.stat().st_size,
    'sha256': digest, 'sha256_matches_hub_lfs': digest == expected,
    'unique_event_ids': len({r['event_id'] for r in rows}),
    'event_types': counts(r['event_type'] for r in rows),
    'severity': counts(r['severity'] for r in rows),
    'actions_by_event_type': {kind: counts(r.get('action') for r in rows if r['event_type'] == kind) for kind in sorted({r['event_type'] for r in rows})},
    'non_null_fields': dict(sorted(present.items())),
    'timestamp_min': min(times).isoformat(), 'timestamp_max': max(times).isoformat(),
    'timestamps_without_timezone': sum(t.tzinfo is None for t in times),
    'adjacent_timestamp_decreases': sum(a > b for a, b in zip(times, times[1:])),
    'unique_sessions': len({r['advanced_metadata']['session_id'] for r in rows}),
    'auth_unique_users': len({r['user'] for r in auth}),
    'auth_unique_source_ips': len({r['src_ip'] for r in auth}),
    'auth_unique_user_ip_pairs': len({(r['user'], r['src_ip']) for r in auth}),
    'max_failed_logins_in_5min_by_user': failed_window(['user']),
    'max_failed_logins_in_5min_by_ip': failed_window(['src_ip']),
    'max_failed_logins_in_5min_by_user_ip': failed_window(['user','src_ip']),
    'destination_ip_placeholder_NA': sum(r.get('dst_ip') == 'N/A' for r in rows),
    'raw_log_contains_noise': sum(' noise=' in r['raw_log'] for r in rows),
}
(ROOT / 'inspection/full_profile.json').write_text(json.dumps(profile, indent=2) + '\n')
print(json.dumps(profile, indent=2))
