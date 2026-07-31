"""Automate your own Perplexity account from Python."""

from .chrome import ChromeNotFoundError, PplxError, ProfileInUseError
from .client import Client

__all__ = ["Client", "PplxError", "ChromeNotFoundError", "ProfileInUseError"]
