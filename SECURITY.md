# Public research edition

The included replay and dashboard use synthetic inputs and in-memory state.
They do not load `.env`, read an existing trading database, sign transactions,
submit orders or send notifications. The server binds to loopback, accepts
loopback Host headers and serves its documentation without external resources.

Public market requests require explicit opt-in. HTTP requests do not inherit
environment proxy credentials or `.netrc` authentication and do not follow
redirects. The optional WebSocket helper also bypasses environment proxies.

Keep wallet material, API keys, account exports, private service settings and
runtime databases outside this repository. Do not reuse a private runtime
directory as this checkout. Images must not include account information,
screenshots of private services or identifying metadata.

CI checks tracked files, image metadata, all available Git history with
Gitleaks, and the tested dependency snapshot with pip-audit. These checks are
useful safeguards; they cannot prove that every possible future change is safe.
