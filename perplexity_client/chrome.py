"""Browser plumbing: launch Google Chrome as an ordinary process, attach over CDP.

Nothing here knows about Perplexity. Playwright must not *launch* the browser --
its bundled Chromium and `channel="chrome"` are both challenged by Cloudflare on
sight, while a normally-launched Chrome is not (docs/M0-findings.md). We add no
automation switches; we just attach to a browser started the ordinary way.

Chrome 136+ refuses --remote-debugging-port on the default profile, so the tool
keeps its own profile directory. That directory, not session.json, is what
actually carries the login.
"""

import contextlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import time

from playwright.sync_api import sync_playwright

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


class PplxError(Exception):
    """Base for every error this tool raises on purpose."""


class ChromeNotFoundError(PplxError):
    pass


class ProfileInUseError(PplxError):
    pass


def config_dir() -> pathlib.Path:
    env = os.environ.get("PPLX_CONFIG_DIR")
    return pathlib.Path(env) if env else pathlib.Path.home() / ".config" / "perplexity-client"


def profile_dir() -> pathlib.Path:
    return config_dir() / "chrome-profile"


def session_path() -> pathlib.Path:
    return config_dir() / "session.json"


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
        "downloading a browser.")


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
        os.kill(pid, 0)  # stale lock from a crashed Chrome -> OSError -> not in use
    except (OSError, ValueError, IndexError, NotImplementedError):
        return None
    return pid


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def save_session(ctx) -> bool:
    """Write storage_state atomically at mode 600. Returns whether it wrote.

    Refuses to write an unauthenticated state: a run that got logged out must not
    clobber a good session file. The file is credential-equivalent, so it is
    created 600 rather than chmod'ed after the fact.
    """
    state = ctx.storage_state()
    if not any("session-token" in c["name"] for c in state.get("cookies", ())):
        return False
    config_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    path = session_path()
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return True


@contextlib.contextmanager
def chrome(headless: bool = True, url: str = "about:blank"):
    """Launch Chrome on the tool's profile, attach over CDP, yield (context, page).

    Saves rotated cookies back on clean exit only, then always reaps the Chrome it
    started -- browser.close() over a CDP connection merely disconnects.
    """
    if pid := profile_owner_pid():
        raise ProfileInUseError(
            f"Chrome is already using {profile_dir()} (pid {pid}). "
            "Quit that window and retry.")
    profile_dir().mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir(), 0o700)
    port = _free_port()
    proc = subprocess.Popen(
        [find_chrome(), f"--remote-debugging-port={port}",
         f"--user-data-dir={profile_dir()}", *COMMON_ARGS,
         *(HEADLESS_ARGS if headless else ()), url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + PORT_TIMEOUT
        while not _port_open(port):
            if proc.poll() is not None:
                raise PplxError(f"Chrome exited immediately (code {proc.returncode})")
            if time.monotonic() > deadline:
                raise PplxError(f"Chrome did not open a debugging port within "
                                f"{PORT_TIMEOUT:.0f}s")
            time.sleep(0.2)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            yield ctx, page
            save_session(ctx)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
