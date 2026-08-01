"""Every error this tool raises on purpose.

Its own module so that `pacing` (which knows nothing about browsers) and `chrome`
(which knows nothing about pacing) can share a base class without importing each
other.
"""


class PplxError(Exception):
    """Base for every error this tool raises on purpose."""


class LocalError(PplxError):
    """This machine is misconfigured -- the account never pushed back.

    `pacing` counts failures to back off with, and it must not count these: they do
    not heal on their own, so every retry of the user's fix would be slower than the
    last. The marker lives here rather than as a tuple in `pacing` so that module
    still needs to know nothing about browsers.
    """


class ChromeNotFoundError(LocalError):
    pass


class ProfileInUseError(LocalError):
    pass


class LockTimeoutError(PplxError):
    pass
