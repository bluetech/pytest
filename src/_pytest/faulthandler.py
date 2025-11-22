from __future__ import annotations

from collections.abc import Generator
import os
import sys

from _pytest.config import Config
from _pytest.config import hookimpl
from _pytest.config.argparsing import Parser


def pytest_addoption(parser: Parser) -> None:
    help_timeout = (
        "Dump the traceback of all threads if a test takes "
        "more than TIMEOUT seconds to finish"
    )
    help_exit_on_timeout = (
        "Exit the test process if a test takes more than "
        "faulthandler_timeout seconds to finish"
    )
    parser.addini("faulthandler_timeout", help_timeout, default=0.0)
    parser.addini(
        "faulthandler_exit_on_timeout", help_exit_on_timeout, type="bool", default=False
    )


def pytest_configure(config: Config) -> None:
    plugin = FaulthandlerPlugin(config)
    config.pluginmanager.register(plugin, "faulthandler-plugin")


def get_stderr_fileno() -> int:
    try:
        fileno = sys.stderr.fileno()
        # The Twisted Logger will return an invalid file descriptor since it is not backed
        # by an FD. So, let's also forward this to the same code path as with pytest-xdist.
        if fileno == -1:
            raise AttributeError()
        return fileno
    except (AttributeError, ValueError):
        # pytest-xdist monkeypatches sys.stderr with an object that is not an actual file.
        # https://docs.python.org/3/library/faulthandler.html#issue-with-file-descriptors
        # This is potentially dangerous, but the best we can do.
        assert sys.__stderr__ is not None
        return sys.__stderr__.fileno()


class FaulthandlerPlugin:
    def __init__(self, config: Config) -> None:
        import faulthandler

        self.config = config

        # at teardown we want to restore the original faulthandler fileno
        # but faulthandler has no api to return the original fileno
        # so here we stash the stderr fileno to be used at teardown
        # sys.stderr and sys.__stderr__ may be closed or patched during the session
        # so we can't rely on their values being good at that point (#11572).
        stderr_fileno = get_stderr_fileno()

        if not faulthandler.is_enabled():
            self.original_stderr_fd = None
        else:
            self.original_stderr_fd = stderr_fileno
        self.stderr_fd = os.dup(stderr_fileno)
        faulthandler.enable(file=self.stderr_fd)

    def pytest_unconfigure(self) -> None:
        import faulthandler

        faulthandler.disable()
        # Close the dup file installed during pytest_configure (__init__).
        if self.stderr_fd is not None:
            os.close(self.stderr_fd)
        # Re-enable the faulthandler if it was originally enabled.
        if self.original_stderr_fd is not None:
            faulthandler.enable(self.original_stderr_fd)

    @hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_protocol(self) -> Generator[None, object, object]:
        timeout = float(self.config.getini("faulthandler_timeout"))
        exit_on_timeout: bool = self.config.getini("faulthandler_exit_on_timeout")
        if timeout > 0:
            import faulthandler

            faulthandler.dump_traceback_later(
                timeout, file=self.stderr_fd, exit=exit_on_timeout
            )
            try:
                return (yield)
            finally:
                faulthandler.cancel_dump_traceback_later()
        else:
            return (yield)

    @hookimpl(tryfirst=True)
    def pytest_enter_pdb(self) -> None:
        """Cancel any traceback dumping due to timeout before entering pdb."""
        import faulthandler

        faulthandler.cancel_dump_traceback_later()

    @hookimpl(tryfirst=True)
    def pytest_exception_interact(self) -> None:
        """Cancel any traceback dumping due to an interactive exception being
        raised."""
        import faulthandler

        faulthandler.cancel_dump_traceback_later()
