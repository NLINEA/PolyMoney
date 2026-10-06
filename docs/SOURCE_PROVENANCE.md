# Source provenance

This edition was prepared on 7 October 2026 from the later local PolyMoney
research code family, which includes April 2026 strategy changes. It retains
actual research modules rather than replacing the project with an unrelated
example. Public Git history starts with this cleaned edition.

| Area | Source relationship | Public changes |
| --- | --- | --- |
| Market scanner, price helpers | Original modules | Credential-free, opt-in HTTP adapter; stricter malformed-data handling; binary outcome/token mapping |
| Risk manager | Original module | Enforce the position percentage cap on direct opens; reject invalid numeric inputs/payouts; correct drawdown threshold interpretation; remove notifications |
| SQLite state store | Original module | Default to in-memory state; add explicit connection cleanup; retain schema/recovery/timeline/health logic |
| Crypto Snipe | Original signal definitions | Keep thresholds, trend/candle helpers and consecutive-loss stop; replace private stop-state DB access with process-local state; omit bot lifecycle adapters |
| Early Panic / BTC Momentum | Original signal definitions | Keep price history and signal/filter logic; use explicit UTC+8 for hour gates; remove private analysis writes and small-sample performance assertions; omit bot lifecycle adapters |
| Sports `resolution_arb` | Original heuristic module | Explain directional exposure and uncalibrated scores; remove unsupported certainty/performance wording; reject expired/nonfinite candidates |
| Public market cache / WebSocket helpers | Original modules | In-memory cache; WebSocket start requires explicit public-data opt-in; no worker starts from the published entrypoints |
| Resolution helper | Original module | Require explicit final resolution status and exact binary endpoint prices; `closed` alone is insufficient |
| Executor | Rewritten for this edition | Offline immediate FAK/FOK depth matcher; no credential, signing or live order client |
| CLI / replay / server | New public entrypoints | In-memory synthetic replay, explicit read-only scan, local read-only dashboard |
| Dashboard | Original visual palette and card/table CSS | New HTML/JS for synthetic research data; no credentials/settings controls or third-party CDN scripts |

The private orchestration entrypoint, account-specific server branches, NAS
adapter, redemption tools, notifications, environment files, logs, database
files and personal wallet/account records were excluded. Superseded strategy
archives and unrelated source trees were not copied.

The included fixture is newly authored synthetic data. `engine` labels identify
the example's subject; they do not mean that the named engine generated those
signals. No private trade histories or aggregate account-loss claims are used
to support the example's output.
