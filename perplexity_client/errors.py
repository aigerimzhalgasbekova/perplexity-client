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


class SessionExpiredError(PplxError):
    """There is no usable login. Re-running `pplx login` is the only fix.

    Raised before a query is spent, never during one -- discovering it mid-stream would
    burn a query on a session that could never have answered.
    """


class ChallengeEncounteredError(PplxError):
    """perplexity.ai served a bot-detection challenge.

    A terminal state on purpose (PRD §8): the tool never solves one, never bypasses one
    and never retries around one. It says so and stops.
    """


class QuotaExhaustedError(PplxError):
    """The account's quota for this mode is used up.

    Refused up front rather than discovered mid-stream. Availability is the only quota
    signal the account has -- no remaining count exists for the modes this tool drives
    (docs/M2-findings.md), so there is nothing to report but "not now".
    """


class IncompleteAnswerError(PplxError):
    """The answer stream ended without its completion signal.

    Raised rather than returned, because PRD §10 rates a truncated answer entering an
    agent pipeline as fact the critical failure of this tool -- it is wrong and it looks
    right. Callers who want the partial text pass `allow_incomplete=True`.
    """


class CitationError(PplxError):
    """A `[n]` marker in the answer has no citation `n`.

    PRD §5 makes this an error rather than a silent drop, because the failure it guards
    is an answer that cites a real URL which does not support the claim -- which reads
    as correct to everything downstream.
    """
