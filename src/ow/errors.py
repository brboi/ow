"""The errors ow raises on purpose, each carrying the exit code `main()` maps it to."""


class OwError(Exception):
    """An expected failure: one message, no traceback, `exit_code`."""

    exit_code = 1


class UsageError(OwError):
    """A usage error, a declined prompt, or a prompt with no TTY and no `-y` (exit 2)."""

    exit_code = 2


class Refused(OwError):
    """At least one repo was refused or failed (exit 1)."""

    exit_code = 1


class PluginError(OwError):
    """A plugin hook raised, or returned something the core refuses (exit 1).

    `plugin` names the plugin to blame (two owners of one path name both, joined by
    " and "), `hook` the hook that was running, `cause` the exception or the problem.
    """

    exit_code = 1

    def __init__(self, plugin: str, hook: str, cause: BaseException | str) -> None:
        self.plugin = plugin
        self.hook = hook
        self.cause = cause
        detail = f"{type(cause).__name__}: {cause}" if isinstance(cause, BaseException) else cause
        super().__init__(f"plugin {plugin} failed in {hook}: {detail}")
