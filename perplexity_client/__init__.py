"""Automate your own Perplexity account from Python."""

from .client import Client
from .errors import ChromeNotFoundError, LockTimeoutError, PplxError, ProfileInUseError

__all__ = ["Client", "PplxError", "ChromeNotFoundError", "ProfileInUseError",
           "LockTimeoutError"]
