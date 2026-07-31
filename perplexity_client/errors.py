"""Every error this tool raises on purpose.

Its own module so that `pacing` (which knows nothing about browsers) and `chrome`
(which knows nothing about pacing) can share a base class without importing each
other.
"""


class PplxError(Exception):
    """Base for every error this tool raises on purpose."""


class ChromeNotFoundError(PplxError):
    pass


class ProfileInUseError(PplxError):
    pass


class LockTimeoutError(PplxError):
    pass
