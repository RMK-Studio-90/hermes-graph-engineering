# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | yes |
| < 1.0 | no |

## Reporting a vulnerability

Please do not open a public issue for security problems.

Report vulnerabilities privately through GitHub's **"Report a vulnerability"** button
(private security advisory) on this repository's **Security** tab. Include the
affected version, a description, reproduction steps and the impact you expect.
You will receive an acknowledgement through the advisory thread, and fixes are
released as patch versions with a changelog entry.

## Security model

- Graph Engineering orchestrates work; it does not grant permissions. `agent` nodes
  are performed by the Hermes agent under Hermes' own tool permissions and approvals.
- `builtin` operations are pure in-process data transformations without filesystem,
  network, subprocess or model access.
- Plan and gate approvals, denials, retries and cancellation are available only as
  slash commands, not through the agent tool. Anyone allowed to run slash commands in
  a Hermes session (for example an authorized messaging-platform user) can approve.
  Restrict access in Hermes accordingly.
- `/ge-create` and `/ge-validate` can read `.json`/`.yaml`/`.yml` spec files that the
  Hermes process can read; parse errors from files are reported without echoing file
  contents.
- Run state is checksummed and the trace is hash-chained. This detects accidental
  corruption and naive tampering; it is not a signature and does not protect against
  someone who can rewrite both state and trace with recomputed hashes.
- Rollback boundaries are declarative markers; the runtime does not undo external
  side effects.
