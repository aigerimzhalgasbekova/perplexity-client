# Contributing

## Testing posture: green CI does not mean the tool works

This is the thing to understand before trusting a test run.

**The unit and adapter suites run against dated, recorded fixtures.** Every fixture in
`spike/fixtures/` carries its capture date in its filename, and that date is the scope
of what a passing suite proves:

> the parser still handles perplexity.ai **as it was on the capture date** — nothing
> more.

Perplexity ships frontend and protocol changes whenever it likes, and this project has
no contract with them. A rename of one JSON key, a changed DOM role, a new terminal
signal: the fixtures keep passing, and the tool is broken for every user.

**Live health is `pplx doctor`'s job.**

```
pplx doctor
```

It spends one real query against your own logged-in account and asserts every invariant
the tool relies on — the completion signal, citations parsed and index-mapped, a thread
id, an observed model, plus Chrome's presence and version. It exits non-zero naming the
invariant that failed, which is what points at *which* part of the frontend moved.

**`doctor` is never run in CI.** CI has no Perplexity account, no logged-in Chrome
profile, and no business spending someone's quota. Run it manually before a release, and
on a schedule against a maintainer account if you want drift caught early.

| | fixtures + `pytest` | `pplx doctor` |
|---|---|---|
| runs in CI | yes | never |
| costs a query | no | one, every run |
| catches a parser regression | yes | yes |
| catches a live-site change | **no** | yes |

## Refreshing fixtures

Fixtures come from real captures, redacted and dated:

```
python spike/capture.py search "what is a quokka"   # writes spike/captures/*.jsonl
python spike/make_fixtures.py spike/captures/search-<ts>.jsonl
python spike/fetch_fixtures.py <thread_slug>        # models, clarifiers, threads
```

Captures themselves are gitignored — they contain a whole session's traffic. Only the
redacted fixtures are committed, and `make_fixtures.py` checks its own redaction
(`author_id`, `author_username`, `author_image`, `read_write_token`) before writing.

**Never delete an old fixture when adding a new one just because it is older.** A
fixture is evidence about a date. If a behaviour changed between two dates, both files
are the record of that, and the test names should say which is which.

## Probes

`spike/probe_*.py` are single-purpose recon scripts kept for provenance, not shipped
code — they are excluded from ruff and mypy. Each one names, in its docstring, the
question it was written to answer and what it cost in queries. Write a new one rather
than editing an old one: the old one is the record of how a claim in `docs/M*-findings.md`
was established.

## House rules

- **All site-specific knowledge lives in `perplexity_client/adapter.py`.** No other
  module may name an endpoint, a JSON key, or a DOM role. A frontend change should be a
  patch to one file.
- **Never spend a query to discover a misconfiguration.** Session, quota, mode and model
  are all checked or confirmed *before* submitting. A query is finite, invisible, and
  the user's.
- **No CAPTCHA or bot-detection bypass, ever** (PRD §8). Not a missing feature — a rule.
  The tool never spoofs a fingerprint, patches `navigator.webdriver`, or retries around
  a challenge; it reports one and stops.
- **An uncertain answer is never returned as a certain one.** Incomplete answers raise;
  unmapped citation markers raise; a substituted model raises. Each of those is a case
  where the output looks right and is not.
- Comments explain *why*, especially where the code looks odd — nearly every strange
  line here is a live-site behaviour someone paid a query to learn.

## Checks

```
uv run ruff check . && uv run ruff format --check .
uv run mypy perplexity_client
uv run pytest
```

## Continuous integration and releases

Continuous integration (CI) — checks run automatically on the project's servers rather
than on your machine — runs on GitHub Actions, defined in `.github/workflows/`.

`ci.yml` runs on every push to `main` and every pull request. It runs
`pre-commit run --all-files`, then `mypy`, then `pytest`. `pre-commit` covers the two
ruff checks above and also the hygiene hooks — `detect-private-key` above all, which is
this repo's only guard against a committed secret. Those hooks otherwise run only for
people who remembered `pre-commit install`, so CI runs them for everyone. `mypy` still
runs separately because the pre-commit hook is the looser one, with
`--ignore-missing-imports` and its own pinned dependencies.

No browser is installed and no query is spent, because every test drives a fake Chrome
DevTools Protocol (CDP) context — the protocol the tool uses to talk to a running
Chrome — and reads the recorded fixtures. `pplx doctor` is not run there, for the
reasons above.

`release.yml` is manual (**Actions → Release → Run workflow**), so that a maintainer can
run `pplx doctor` against a real account first and confirm the live site still works.
It refuses to run from anything but `main`, and it re-runs the full `ci.yml` gate before
tagging: CI on `main` is a separate run that this job neither waits for nor reads, so
without that gate a release could be cut from a red commit.

It uses [commitizen](https://commitizen-tools.github.io/commitizen/), already a dev
dependency and already enforcing conventional commit messages through the `commit-msg`
hook. `cz bump` reads the commits since the last tag and picks the new version from
them — a `fix:` commit bumps the patch number, a `feat:` the minor, a breaking change
the major — then writes it into `pyproject.toml`, updates `CHANGELOG.md`, commits, tags
`vX.Y.Z`, and opens a GitHub Release with that version's changelog section.

So the commit message *is* the version bump. `chore:` and `docs:` commits release
nothing; a `feat:` that lands as `chore:` is a release that never happens.

### Three things the release job gets right on purpose

Each of these is a way it silently produced a wrong release before, so do not simplify
them away:

- **`annotated_tag = true`** in `[tool.commitizen]`. commitizen creates *lightweight*
  tags by default, and `git push --follow-tags` pushes only *annotated* ones — so the
  bump commit reached `main` without its tag, and `gh release create` then invented a
  tag at whatever `main` pointed to at that moment. `--verify-tag` on the release makes
  that failure loud rather than silent.
- **`version_files`** in `[tool.commitizen]`. Without it `cz bump` rewrites only
  `[tool.commitizen].version`, and `[project].version` — the one that ships — stays
  `0.1.0` in every release ever cut. Because `uv.lock` pins this package's own version
  too, the job runs `uv lock` and amends it into the bump commit; left stale it fails
  `uv sync --locked` on `main`, which is the first step of both workflows.
- **Exit code 3 is tolerated** on `cz bump`. That is commitizen's `NO_COMMITS_FOUND`,
  which is the resume path: if a previous run pushed the bump and tag but failed before
  publishing, there is nothing left to bump and the job should carry on to the release
  step. The release step checks `gh release view` first, so re-running it is safe.

The workflow pushes a commit and a tag to `main`. If `main` is protected, its token
needs permission to push, or the branch rule needs to allow it.
