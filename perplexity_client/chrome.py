"""Browser plumbing: launch Google Chrome as an ordinary process, attach over CDP.

Nothing here knows about Perplexity. Playwright must not *launch* the browser --
its bundled Chromium and `channel="chrome"` are both challenged by Cloudflare on
sight, while a normally-launched Chrome is not (docs/M0-findings.md). We add no
automation switches; we just attach to a browser started the ordinary way.

Chrome 136+ refuses --remote-debugging-port on the default profile, so the tool
keeps its own profile directory. That directory is what carries the login: no
copy of the session is exported, so there is no second credential to protect.
"""

import contextlib
import os
import pathlib
import shutil
import subprocess
import time
from collections.abc import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .errors import ChromeNotFoundError, PplxError, ProfileInUseError
from .pacing import paced

# Google Chrome only, deliberately: M0 established that *this* browser is not
# challenged. A Chromium build may well behave differently under Cloudflare, so
# pointing at one is an explicit PPLX_CHROME opt-in rather than a silent fallback.
CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
CHROME_NAMES = ("google-chrome", "google-chrome-stable")
# Chrome 132 removed old headless; --headless=new and --headless are now the same mode.
HEADLESS_ARGS = ("--headless=new", "--window-size=1280,900")
COMMON_ARGS = ("--no-first-run", "--no-default-browser-check")
PORT_TIMEOUT = 30.0


def config_dir() -> pathlib.Path:
    env = os.environ.get("PPLX_CONFIG_DIR")
    return (
        pathlib.Path(env)
        if env
        else pathlib.Path.home() / ".config" / "perplexity-client"
    )


def profile_dir() -> pathlib.Path:
    return config_dir() / "chrome-profile"


def lock_path() -> pathlib.Path:
    return config_dir() / "pplx.lock"


def find_chrome() -> str:
    if env := os.environ.get("PPLX_CHROME"):
        return env
    for name in CHROME_NAMES:
        if found := shutil.which(name):
            return found
    for path in CHROME_PATHS:
        if os.path.exists(path):
            return path
    raise ChromeNotFoundError(
        "Google Chrome not found. Install it, or set PPLX_CHROME to the binary. "
        "Chrome is a prerequisite: the tool drives your own Chrome rather than "
        "downloading a browser."
    )


def chrome_version() -> str:
    out = subprocess.run([find_chrome(), "--version"], capture_output=True, text=True)
    return out.stdout.strip() or "unknown"


def profile_owner_pid() -> int | None:
    """PID of a live Chrome holding our profile, if any.

    Chrome's SingletonLock is a symlink to "<host>-<pid>". A second Chrome on a
    locked profile hands off to the first and exits *without* opening a debugging
    port, so without this check the failure is an opaque port timeout.
    """
    try:
        pid = int(os.readlink(profile_dir() / "SingletonLock").rsplit("-", 1)[1])
    except OSError, ValueError, IndexError, NotImplementedError:
        return None
    try:
        os.kill(pid, 0)
    except PermissionError:
        return pid  # alive, just not ours -- the one OSError that proves it exists
    except OSError:
        return None  # stale lock from a crashed Chrome
    return pid


def _read_port(path: pathlib.Path) -> int | None:
    """Port from Chrome's DevToolsActivePort, or None until it is written."""
    try:
        return int(path.read_text().split("\n", 1)[0])
    except OSError, ValueError:
        return None


@contextlib.contextmanager
def chrome(
    headless: bool = True, url: str = "about:blank", interval: float = 0.0
) -> Iterator[tuple[BrowserContext, Page]]:
    """Launch Chrome on the tool's profile, attach over CDP, yield (context, page).

    Runs under the advisory lock for its whole life. That is not only pacing: two
    Chromes cannot share one profile directory, so without the lock a concurrent run
    collides rather than queues. `interval` is the pacing floor -- 0 for a page load,
    `pacing.default_interval()` for a caller that spends a query.

    Chrome rotates cookies in its own profile directory, so nothing here has to
    save them. Always reaps the Chrome it started -- browser.close() over a CDP
    connection merely disconnects.
    """
    with paced(lock_path(), interval), _launched(headless, url) as attached:
        yield attached


@contextlib.contextmanager
def _launched(headless: bool, url: str) -> Iterator[tuple[BrowserContext, Page]]:
    # Checked inside the lock, so this can no longer be one of our own runs: it is a
    # Chrome someone opened on the profile by hand, which no amount of waiting fixes.
    if pid := profile_owner_pid():
        raise ProfileInUseError(
            f"Chrome is already using {profile_dir()} (pid {pid}). "
            f"Quit that Chrome window, or run: kill {pid}"
        )
    profile_dir().mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir(), 0o700)
    # Let Chrome pick the port and tell us: binding one ourselves first would be a
    # race, and attaching to whatever won it means driving someone else's browser.
    port_file = profile_dir() / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [
            find_chrome(),
            "--remote-debugging-port=0",
            f"--user-data-dir={profile_dir()}",
            *COMMON_ARGS,
            *(HEADLESS_ARGS if headless else ()),
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + PORT_TIMEOUT
        while (port := _read_port(port_file)) is None:
            if proc.poll() is not None:
                raise PplxError(f"Chrome exited immediately (code {proc.returncode})")
            if time.monotonic() > deadline:
                raise PplxError(
                    f"Chrome did not open a debugging port within {PORT_TIMEOUT:.0f}s"
                )
            time.sleep(0.2)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            yield ctx, page
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
