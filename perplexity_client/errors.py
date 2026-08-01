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

    Deliberately *not* a `LocalError`, though it shares that class's "waiting will not
    fix it" shape and so accrues pacing backoff. Two reasons. An account being
    throttled or blocked can surface here rather than as `challenged` -- M1 found the
    auth probe answers `200 {}` from behind an interstitial -- and that is precisely a
    case the backoff should slow down. And the cost of counting it is small: the 20s
    interval floor already exceeds the backoff until the fourth consecutive failure, so
    a user who hits this once, re-logs in and retries pays nothing. Only a loop that
    kept asking against a dead session pays, and slowing that down is the point.
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


class ModelUnavailableError(PplxError):
    """The requested model is not one this account may pick.

    A `LocalError` in spirit -- waiting will not fix it -- but deliberately not one:
    it is raised before a query is spent, from the picker, and classing it local would
    exempt a caller looping on a typo'd model name from any pacing at all.

    Entitlement is read off the DOM rather than guessed: a model the plan can use is a
    `menuitemradio`, one it cannot is a plain `menuitem` with a "Max" badge
    (docs/M4-M8-findings.md). So this is the site's own answer, not our arithmetic.
    """


class ModelMismatchError(PplxError):
    """A different model served the answer than the one that was requested.

    Observed on the first try (docs/M4-M8-findings.md): requesting Sonar 2 sent
    `model_preference: "experimental"` and the terminal frame came back
    `display_model: "turbo"`. PRD §10 rates a silent model swap Medium precisely
    because nothing downstream can see it -- the answer is fluent either way.

    Never raised for `model="best"`, where auto-selection is the point (US-6).
    """


class ClarificationRequiredError(PplxError):
    """Deep Research stopped to ask clarifying questions and `on_clarify="raise"`.

    Carries the parsed questions so a caller can answer them on a later call rather
    than re-running the query blind. The default is to skip, because an unattended
    client is the primary use case (PRD §5).
    """

    def __init__(self, message: str, questions: list[object] | None = None) -> None:
        super().__init__(message)
        self.questions = questions or []


class CitationError(PplxError):
    """A `[n]` marker in the answer has no citation `n`.

    PRD §5 makes this an error rather than a silent drop, because the failure it guards
    is an answer that cites a real URL which does not support the claim -- which reads
    as correct to everything downstream.
    """
