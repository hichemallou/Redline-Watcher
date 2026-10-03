# Advanced SIEM dataset fit for ClawWatch

Reviewed on 2026-10-03 against `/Users/binzhang/Downloads/cybersecurity-agent-roadmap.md`.

**Decision: partial fit. Downloaded as a supporting corpus for ingestion, historical search, and single-event summaries. It is not sufficient as the main dataset for the roadmap's correlated-incident demo or for measuring detection accuracy.**

## Inspection and download

- Source: https://huggingface.co/datasets/darkknight25/Advanced_SIEM_Dataset
- Publisher describes the data as synthetic and declares MIT licensing in the dataset card. The downloaded card is in `raw/README.md`.
- Revision: `72201c8d533f72039850ff0250e68a039283440d`.
- Inspected the Dataset Viewer schema and first rows, then sampled 1,000 rows before downloading the full corpus: ten randomly chosen, non-overlapping blocks of 100 rows, seed 42. This is a block sample, not 1,000 independent random draws.
- Sample offsets and row indices: `inspection/sample_indices.json`; sampled records: `inspection/sample_1000.jsonl`; sample statistics and examples: `inspection/sample_profile.json`.
- Sample contained 136 auth events: 18 failed, 21 success, 20 locked, 27 bypass, 22 timeout, and 28 challenge.
- Original file: `raw/advanced_siem_dataset.jsonl`, 93,724,615 bytes (93.72 MB; 89.38 MiB).
- All 100,000 lines parsed as JSON; all 100,000 event IDs are unique.
- SHA-256 matches the Hub LFS object: `b30902649de9197376e7b245045c6f6721ba3039edc1b7cac3474cc2ad0c4f21`.
- Full-corpus measurements: `inspection/full_profile.json`. Reproduce with `rtk proxy python3 data/advanced_siem/inspection/profile_dataset.py` from the repository root.

## Match to your roadmap

| Requirement | Result | Implication |
| --- | --- | --- |
| JSONL historical import with traceable evidence | Good | Stable IDs, timestamps, event types, descriptions, and raw-log strings are present on every record. |
| Authentication events | Good at individual-event level | 12,516 auth records, including 2,086 failures and 2,067 successes. |
| Failed-login burst followed by success and privileged action | Does not support the planned rule unchanged | Maximum failures within any five-minute window is **one**, whether grouped by user, source IP, or user/IP pair. The roadmap's ten-failure threshold cannot fire. These checks omit service grouping, so adding that constraint cannot increase the count. |
| Linked session investigation | Poor | All 100,000 session IDs are unique. All 12,516 auth user/IP pairs are unique. Repeated usernames alone should not be treated as evidence of a coherent attack chain. |
| Repeated access denied to sensitive application endpoints | Missing | Firewall `deny` exists, but that describes network traffic. There is no application route plus HTTP response/access-status schema. Endpoint records describe host/process/file activity, not web API endpoints. |
| Service ownership and two-team routing | Missing | No service, environment, owner, or Slack destination fields. `source` names a security product/version, not an affected backend service. |
| Historical-to-live demo | Requires separate scenario data | Existing rows can be replayed after timestamp handling, but replay does not supply missing linked incidents. |
| Detection accuracy, benign controls, incident ground truth | Insufficient | No incident-level truth labels or validated sequence annotations. Supplied severity, risk, confidence, and ATT&CK text are synthetic annotations, not verified outcomes. |

## Full-corpus composition

| Event type | Rows |
| --- | ---: |
| auth | 12,516 |
| endpoint | 12,589 |
| cloud | 12,511 |
| firewall | 12,448 |
| ids_alert | 12,500 |
| network | 12,335 |
| iot | 12,434 |
| ai | 12,667 |

For a focused first import, start with authentication records. Endpoint and cloud events can support additional summary examples, but cannot be assumed to connect to the auth events.

## Mapping to the planned schema

| ClawWatch field | Dataset field / required decision |
| --- | --- |
| `event_id` | Preserve `event_id`. |
| `timestamp` | Parse `timestamp`; explicitly choose and record a timezone policy before replay. |
| `service` | Absent. Use an external, explicitly synthetic demo mapping or keep ownership unresolved. Do not map the SIEM vendor to a backend owner. |
| `environment` | Absent. Configure a demo/import environment outside the original record. |
| `event_type` | Preserve the original category; `auth` is directly relevant. |
| `actor` | `user`, when present. |
| `source_ip` | `src_ip`, when present. |
| `action` | `action`; for auth, a future normalizer could use `action=login` and preserve the original action as `status`. |
| `status` | Auth `action` supplies outcomes such as `failed` or `success`. No general status field exists; avoid inventing success for other categories. |
| redacted message | Redact `description` / `raw_log` before model input and notifications. |
| provenance | Retain dataset revision, original row index or line number, and event ID. Label outputs synthetic and historical/replayed. |

## Data quality and evaluation limits

- All timestamps lack timezone information. Their range is 2020-07-12 through 2030-07-10; 49,900 adjacent row pairs decrease in time. File order is unsuitable for chronological replay without sorting. Preserve originals if rebasing timestamps into a demo window.
- `user` is present on 50,283 rows and `src_ip` on 49,799 rows. Different categories have different fields; missing values are expected.
- 3,709 destination IP values are the literal string `N/A`; treat them as missing, not as real addresses.
- 20,107 raw logs contain a `noise=` suffix. These are useful parser cases, but are not by themselves a prompt-injection evaluation set.
- The samples include loose semantic pairings, such as a DDoS alert categorized as `Evasion`. Do not use supplied threat labels or scores as authoritative conclusions.
- Severity appears inside `raw_log`, and descriptions sometimes name an attack outright. Using these inputs to predict the same supplied labels risks circular evaluation. Keep annotations separate when evaluating independent detection.
- An `ai` event with action `prompt_injection` is not necessarily an actual instruction-bearing log payload. The roadmap still needs explicit adversarial-text fixtures.

## Recommended use on build day

1. Use this corpus for import/parsing checks, bounded history retrieval, evidence-ID validation, and summaries. Begin with auth; add categories only as needed.
2. Build a separately labeled scenario set with repeated failures, a linked success, and a privileged action for one identity/service; include the sensitive-API denied-access case and benign controls.
3. Add the explicit ownership map, failure cases, duplicate replays, and instruction-like log text described in the roadmap. Keep scenario generation and custom agent implementation on build day as planned.
4. Report metrics on the labeled scenarios separately from corpus-processing statistics. Do not claim this corpus demonstrates production detection accuracy.

No events were rewritten and no custom agent or synthetic incident fixtures were built during this inspection. Raw data and the larger sample files are ignored by Git; the assessment and compact inspection metadata can be versioned.
