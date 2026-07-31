# perplexity-client

Automate **your own** Perplexity account from Python and the shell.

Perplexity Pro does not include API access. If you already pay for Pro and want to
script your own queries, this drives a real, manually-authenticated browser session
on your machine — the same thing you would do by hand, done from a script. It is not
an API substitute, and it is not affiliated with Perplexity.

Status: **milestone 1** — session bootstrap only (`login`, `status`). Querying lands
in milestone 3; see `docs/PRD.md`.

## Requirements

- Python 3.10+
- **Google Chrome**, installed. The tool launches your own Chrome as an ordinary
  process and attaches over CDP; it never downloads a browser and never adds
  automation switches.

## Install

```sh
pip install git+https://github.com/<org>/perplexity-client
playwright install-deps   # not needed on macOS/Windows
```

## Use

```sh
pplx login     # one-time manual login in a visible Chrome window
pplx status    # ok | no-session | expired | challenged
```

```python
from perplexity_client import Client

Client().status()   # "ok"
```

`status` costs one page load, not one query. Exit codes: `0` ok, `1` session not
usable, `2` tool error.

## What it stores

Under `~/.config/perplexity-client/` (override with `PPLX_CONFIG_DIR`):

| Path | What |
|---|---|
| `chrome-profile/` | The Chrome profile that carries your login |
| `session.json` | Exported `storage_state`, mode 600 |

**Both are password-equivalent.** Anyone who can read them can use your account.
The tool never sees, types or stores a password, 2FA code or SSO credential — login
is always manual, in a real browser window.

## What it will not do

No CAPTCHA or bot-detection bypass, ever. If Perplexity challenges the session, the
tool reports `challenged` and stops. No multi-account use, no resale, no parallel
hammering of one account — same-account runs are serialized on purpose. You are
responsible for your own account's compliance with Perplexity's terms.
