"""
per-test stdout/stderr capturing mechanism.

"""
import collections
import contextlib
import io
import os
import sys
from io import UnsupportedOperation
from tempfile import TemporaryFile
from typing import Optional
from typing import TextIO
from typing import Tuple

import pytest
from _pytest.compat import TYPE_CHECKING
from _pytest.config import Config

if TYPE_CHECKING:
    from typing_extensions import Literal

    _CaptureMethod = Literal["fd", "sys", "no", "tee-sys"]


def pytest_addoption(parser):
    group = parser.getgroup("general")
    group._addoption(
        "--capture",
        action="store",
        default="fd",
        metavar="method",
        choices=["fd", "sys", "no", "tee-sys"],
        help="per-test capturing method: one of fd|sys|no|tee-sys.",
    )
    group._addoption(
        "-s",
        action="store_const",
        const="no",
        dest="capture",
        help="shortcut for --capture=no.",
    )


def _colorama_workaround():
    """
    Ensure colorama is imported so that it attaches to the correct stdio
    handles on Windows.

    colorama uses the terminal on import time. So if something does the
    first import of colorama while I/O capture is active, colorama will
    fail in various ways.
    """
    if sys.platform.startswith("win32"):
        try:
            import colorama  # noqa: F401
        except ImportError:
            pass


def _readline_workaround():
    """
    Ensure readline is imported so that it attaches to the correct stdio
    handles on Windows.

    Pdb uses readline support where available--when not running from the Python
    prompt, the readline module is not imported until running the pdb REPL.  If
    running pytest with the --pdb option this means the readline module is not
    imported until after I/O capture has been started.

    This is a problem for pyreadline, which is often used to implement readline
    support on Windows, as it does not attach to the correct handles for stdout
    and/or stdin if they have been redirected by the FDCapture mechanism.  This
    workaround ensures that readline is imported before I/O capture is setup so
    that it can attach to the actual stdin/out for the console.

    See https://github.com/pytest-dev/pytest/pull/1281
    """
    if sys.platform.startswith("win32"):
        try:
            import readline  # noqa: F401
        except ImportError:
            pass


def _py36_windowsconsoleio_workaround(stream):
    """
    Python 3.6 implemented unicode console handling for Windows. This works
    by reading/writing to the raw console handle using
    ``{Read,Write}ConsoleW``.

    The problem is that we are going to ``dup2`` over the stdio file
    descriptors when doing ``FDCapture`` and this will ``CloseHandle`` the
    handles used by Python to write to the console. Though there is still some
    weirdness and the console handle seems to only be closed randomly and not
    on the first call to ``CloseHandle``, or maybe it gets reopened with the
    same handle value when we suspend capturing.

    The workaround in this case will reopen stdio with a different fd which
    also means a different handle by replicating the logic in
    "Py_lifecycle.c:initstdio/create_stdio".

    :param stream: in practice ``sys.stdout`` or ``sys.stderr``, but given
        here as parameter for unittesting purposes.

    See https://github.com/pytest-dev/py/issues/103
    """
    if (
        not sys.platform.startswith("win32")
        or sys.version_info[:2] < (3, 6)
        or hasattr(sys, "pypy_version_info")
    ):
        return

    # bail out if ``stream`` doesn't seem like a proper ``io`` stream (#2666)
    if not hasattr(stream, "buffer"):
        return

    buffered = hasattr(stream.buffer, "raw")
    raw_stdout = stream.buffer.raw if buffered else stream.buffer

    if not isinstance(raw_stdout, io._WindowsConsoleIO):
        return

    def _reopen_stdio(f, mode):
        if not buffered and mode[0] == "w":
            buffering = 0
        else:
            buffering = -1

        return io.TextIOWrapper(
            open(os.dup(f.fileno()), mode, buffering),
            f.encoding,
            f.errors,
            f.newlines,
            f.line_buffering,
        )

    sys.stdin = _reopen_stdio(sys.stdin, "rb")
    sys.stdout = _reopen_stdio(sys.stdout, "wb")
    sys.stderr = _reopen_stdio(sys.stderr, "wb")


@pytest.hookimpl(hookwrapper=True)
def pytest_load_initial_conftests(early_config: Config):
    ns = early_config.known_args_namespace
    if ns.capture == "fd":
        _py36_windowsconsoleio_workaround(sys.stdout)
    _colorama_workaround()
    _readline_workaround()
    pluginmanager = early_config.pluginmanager
    capman = CaptureManager(ns.capture)
    pluginmanager.register(capman, "capturemanager")

    # make sure that capturemanager is properly reset at final shutdown
    early_config.add_cleanup(capman.stop_global_capturing)

    # finally trigger conftest loading but while capturing (issue93)
    capman.start_global_capturing()
    outcome = yield
    capman.suspend_global_capture()
    if outcome.excinfo is not None:
        out, err = capman.read_global_capture()
        sys.stdout.write(out)
        sys.stderr.write(err)


# IO Helpers.


class EncodedFile(io.TextIOWrapper):
    __slots__ = ()

    @property
    def name(self) -> str:
        # Ensure that file.name is a string. Workaround for a Python bug
        # fixed in >=3.7.4: https://bugs.python.org/issue36015
        return repr(self.buffer)

    @property
    def mode(self) -> str:
        # TextIOWrapper doesn't expose a mode, but at least some of our
        # tests check it.
        return self.buffer.mode.replace("b", "")


class CaptureIO(io.TextIOWrapper):
    def __init__(self) -> None:
        super().__init__(io.BytesIO(), encoding="UTF-8", newline="", write_through=True)

    def getvalue(self) -> str:
        assert isinstance(self.buffer, io.BytesIO)
        return self.buffer.getvalue().decode("UTF-8")


class TeeCaptureIO(CaptureIO):
    def __init__(self, other: TextIO) -> None:
        self._other = other
        super().__init__()

    def write(self, s) -> int:
        super().write(s)
        return self._other.write(s)


class DontReadFromInput:
    encoding = None

    def read(self, *args):
        raise OSError(
            "pytest: reading from stdin while output is captured!  Consider using `-s`."
        )

    readline = read
    readlines = read
    __next__ = read

    def __iter__(self):
        return self

    def fileno(self):
        raise UnsupportedOperation("redirected stdin is pseudofile, has no fileno()")

    def isatty(self):
        return False

    def close(self):
        pass

    @property
    def buffer(self):
        return self


# Capture classes.


patchsysdict = {0: "stdin", 1: "stdout", 2: "stderr"}


class NoCapture:
    EMPTY_BUFFER = None
    __init__ = start = done = suspend = resume = lambda *args: None


class SysCaptureBinary:

    EMPTY_BUFFER = b""

    def __init__(self, fd, tmpfile=None, *, tee=False):
        name = patchsysdict[fd]
        self._old = getattr(sys, name)
        self.name = name
        if tmpfile is None:
            if name == "stdin":
                tmpfile = DontReadFromInput()
            else:
                tmpfile = CaptureIO() if not tee else TeeCaptureIO(self._old)
        self.tmpfile = tmpfile
        self._state = "initialized"

    def repr(self, class_name: str) -> str:
        return "<{} {} _old={} _state={!r} tmpfile={!r}>".format(
            class_name,
            self.name,
            hasattr(self, "_old") and repr(self._old) or "<UNSET>",
            self._state,
            self.tmpfile,
        )

    def __repr__(self) -> str:
        return "<{} {} _old={} _state={!r} tmpfile={!r}>".format(
            self.__class__.__name__,
            self.name,
            hasattr(self, "_old") and repr(self._old) or "<UNSET>",
            self._state,
            self.tmpfile,
        )

    def _assert_state(self, op: str, states: Tuple[str, ...]) -> None:
        assert (
            self._state in states
        ), "cannot {} in state {!r}: expected one of {}".format(
            op, self._state, ", ".join(states)
        )

    def start(self):
        self._assert_state("start", ("initialized",))
        setattr(sys, self.name, self.tmpfile)
        self._state = "started"

    def snap(self):
        self._assert_state("snap", ("started", "suspended"))
        self.tmpfile.seek(0)
        res = self.tmpfile.buffer.read()
        self.tmpfile.seek(0)
        self.tmpfile.truncate()
        return res

    def done(self):
        self._assert_state("done", ("initialized", "started", "suspended", "done"))
        if self._state == "done":
            return
        setattr(sys, self.name, self._old)
        del self._old
        self.tmpfile.close()
        self._state = "done"

    def suspend(self):
        self._assert_state("suspend", ("started", "suspended"))
        setattr(sys, self.name, self._old)
        self._state = "suspended"

    def resume(self):
        self._assert_state("resume", ("started", "suspended"))
        if self._state == "started":
            return
        setattr(sys, self.name, self.tmpfile)
        self._state = "started"

    def writeorg(self, data):
        self._assert_state("writeorg", ("started", "suspended"))
        self._old.flush()
        self._old.buffer.write(data)
        self._old.buffer.flush()


class SysCapture:
    EMPTY_BUFFER = ""

    def __init__(self, fd: int, tmpfile=None, *, tee: bool = False) -> None:
        self._cap = SysCaptureBinary(fd, tmpfile, tee=tee)

    def __repr__(self) -> str:
        return self._cap.repr(self.__class__.__name__)

    def start(self):
        self._cap.start()

    def snap(self):
        # XXX use encoding of original stream
        return self._cap.snap().decode("utf-8", errors="replace")

    def done(self):
        self._cap.done()

    def suspend(self):
        self._cap.suspend()

    def resume(self):
        self._cap.resume()

    def writeorg(self, data):
        """ write to original file descriptor. """
        # XXX use encoding of original stream
        self._cap.writeorg(data.encode("utf-8"))


class FDCaptureBinary:
    """Capture IO to/from a given os-level filedescriptor.

    snap() produces `bytes`
    """

    EMPTY_BUFFER = b""

    def __init__(self, targetfd):
        self.targetfd = targetfd
        self._state = "initialized"

    def repr(self, class_name: str) -> str:
        return "<{} {} oldfd={} _state={!r} tmpfile={!r}>".format(
            class_name, self.targetfd, self.targetfd_save, self._state, self.tmpfile,
        )

    def __repr__(self) -> str:
        return self.repr(self.__class__.__name__)

    def _assert_state(self, op: str, states: Tuple[str, ...]) -> None:
        assert (
            self._state in states
        ), "cannot {} in state {!r}: expected one of {}".format(
            op, self._state, ", ".join(states)
        )

    def start(self):
        """ Start capturing on targetfd using memorized tmpfile. """
        self._assert_state("start", ("initialized",))

        try:
            os.fstat(self.targetfd)
        except OSError:
            # FD capturing is conceptually simple -- create a temporary file,
            # redirect the FD to it, redirect back when done. But when the
            # target FD is invalid it throws a wrench into this loveley scheme.
            #
            # Tests themselves shouldn't care if the FD is valid, FD capturing
            # should work regardless of external circumstances. So falling back
            # to just sys capturing is not a good option.
            #
            # Further complications are the need to support suspend() and the
            # possibility of FD reuse (e.g. the tmpfile getting the very same
            # target FD). The following approach is robust, I believe.
            self.targetfd_invalid = os.open(os.devnull, os.O_RDWR)
            os.dup2(self.targetfd_invalid, self.targetfd)
        else:
            self.targetfd_invalid = None
        self.targetfd_save = os.dup(self.targetfd)

        if self.targetfd == 0:
            self.tmpfile = open(os.devnull)
            self.syscapture = SysCapture(self.targetfd)
        else:
            self.tmpfile = EncodedFile(
                TemporaryFile(buffering=0),
                encoding="utf-8",
                errors="replace",
                write_through=True,
            )
            if self.targetfd in patchsysdict:
                self.syscapture = SysCapture(self.targetfd, self.tmpfile)
            else:
                self.syscapture = NoCapture()

        os.dup2(self.tmpfile.fileno(), self.targetfd)
        self.syscapture.start()
        self._state = "started"

    def snap(self):
        self._assert_state("snap", ("started", "suspended"))
        self.tmpfile.seek(0)
        res = self.tmpfile.buffer.read()
        self.tmpfile.seek(0)
        self.tmpfile.truncate()
        return res

    def done(self):
        """ stop capturing, restore streams, return original capture file,
        seeked to position zero. """
        self._assert_state("done", ("initialized", "started", "suspended", "done"))
        if self._state == "done":
            return
        if self._state != "initialized":
            os.dup2(self.targetfd_save, self.targetfd)
            os.close(self.targetfd_save)
            if self.targetfd_invalid is not None:
                if self.targetfd_invalid != self.targetfd:
                    os.close(self.targetfd)
                os.close(self.targetfd_invalid)
            self.syscapture.done()
            self.tmpfile.close()
        self._state = "done"

    def suspend(self):
        self._assert_state("suspend", ("started", "suspended"))
        if self._state == "suspended":
            return
        self.syscapture.suspend()
        os.dup2(self.targetfd_save, self.targetfd)
        self._state = "suspended"

    def resume(self):
        self._assert_state("resume", ("started", "suspended"))
        if self._state == "started":
            return
        self.syscapture.resume()
        os.dup2(self.tmpfile.fileno(), self.targetfd)
        self._state = "started"

    def writeorg(self, data):
        """ write to original file descriptor. """
        self._assert_state("writeorg", ("started", "suspended"))
        os.write(self.targetfd_save, data)


class FDCapture:
    """Capture IO to/from a given os-level filedescriptor.

    snap() produces text
    """

    EMPTY_BUFFER = ""

    def __init__(self, targetfd: int) -> None:
        self._cap = FDCaptureBinary(targetfd)

    def __repr__(self) -> str:
        return self._cap.repr(self.__class__.__name__)

    def start(self):
        """ Start capturing on targetfd using memorized tmpfile. """
        self._cap.start()

    def snap(self):
        self._assert_state("snap", ("started", "suspended"))
        # XXX use encoding of original stream
        return self._cap.snap().decode("utf-8", errors="replace")

    def done(self):
        self._cap.done()

    def suspend(self):
        self._cap.suspend()

    def resume(self):
        self._cap.resume()

    def writeorg(self, data):
        """ write to original file descriptor. """
        # XXX use encoding of original stream
        self._cap.writeorg(data.encode("utf-8"))


# MultiCapture

CaptureResult = collections.namedtuple("CaptureResult", ["out", "err"])


class MultiCapture:
    _state = None
    _in_suspended = False

    def __init__(self, in_, out, err) -> None:
        self.in_ = in_
        self.out = out
        self.err = err

    def __repr__(self):
        return "<MultiCapture out={!r} err={!r} in_={!r} _state={!r} _in_suspended={!r}>".format(
            self.out, self.err, self.in_, self._state, self._in_suspended,
        )

    def start_capturing(self):
        self._state = "started"
        if self.in_:
            self.in_.start()
        if self.out:
            self.out.start()
        if self.err:
            self.err.start()

    def pop_outerr_to_orig(self):
        """ pop current snapshot out/err capture and flush to orig streams. """
        out, err = self.readouterr()
        if out:
            self.out.writeorg(out)
        if err:
            self.err.writeorg(err)
        return out, err

    def suspend_capturing(self, in_=False):
        self._state = "suspended"
        if self.out:
            self.out.suspend()
        if self.err:
            self.err.suspend()
        if in_ and self.in_:
            self.in_.suspend()
            self._in_suspended = True

    def resume_capturing(self):
        self._state = "resumed"
        if self.out:
            self.out.resume()
        if self.err:
            self.err.resume()
        if self._in_suspended:
            self.in_.resume()
            self._in_suspended = False

    def stop_capturing(self):
        """ stop capturing and reset capturing streams """
        if self._state == "stopped":
            raise ValueError("was already stopped")
        self._state = "stopped"
        if self.out:
            self.out.done()
        if self.err:
            self.err.done()
        if self.in_:
            self.in_.done()

    def readouterr(self) -> CaptureResult:
        if self.out:
            out = self.out.snap()
        else:
            out = ""
        if self.err:
            err = self.err.snap()
        else:
            err = ""
        return CaptureResult(out, err)


def _get_multicapture(method: "_CaptureMethod") -> MultiCapture:
    if method == "fd":
        return MultiCapture(in_=FDCapture(0), out=FDCapture(1), err=FDCapture(2))
    elif method == "sys":
        return MultiCapture(in_=SysCapture(0), out=SysCapture(1), err=SysCapture(2))
    elif method == "no":
        return MultiCapture(in_=None, out=None, err=None)
    elif method == "tee-sys":
        return MultiCapture(
            in_=None, out=SysCapture(1, tee=True), err=SysCapture(2, tee=True)
        )
    raise ValueError("unknown capturing method: {!r}".format(method))


# CaptureManager and CaptureFixture


class CaptureManager:
    """
    Capture plugin, manages that the appropriate capture method is enabled/disabled during collection and each
    test phase (setup, call, teardown). After each of those points, the captured output is obtained and
    attached to the collection/runtest report.

    There are two levels of capture:
    * global: which is enabled by default and can be suppressed by the ``-s`` option. This is always enabled/disabled
      during collection and each test phase.
    * fixture: when a test function or one of its fixture depend on the ``capsys`` or ``capfd`` fixtures. In this
      case special handling is needed to ensure the fixtures take precedence over the global capture.
    """

    def __init__(self, method: "_CaptureMethod") -> None:
        self._method = method
        self._global_capturing = None
        self._capture_fixture = None  # type: Optional[CaptureFixture]

    def __repr__(self):
        return "<CaptureManager _method={!r} _global_capturing={!r} _capture_fixture={!r}>".format(
            self._method, self._global_capturing, self._capture_fixture
        )

    def is_capturing(self):
        if self.is_globally_capturing():
            return "global"
        if self._capture_fixture:
            return "fixture %s" % self._capture_fixture.request.fixturename
        return False

    # Global capturing control

    def is_globally_capturing(self):
        return self._method != "no"

    def start_global_capturing(self):
        assert self._global_capturing is None
        self._global_capturing = _get_multicapture(self._method)
        self._global_capturing.start_capturing()

    def stop_global_capturing(self):
        if self._global_capturing is not None:
            self._global_capturing.pop_outerr_to_orig()
            self._global_capturing.stop_capturing()
            self._global_capturing = None

    def resume_global_capture(self):
        # During teardown of the python process, and on rare occasions, capture
        # attributes can be `None` while trying to resume global capture.
        if self._global_capturing is not None:
            self._global_capturing.resume_capturing()

    def suspend_global_capture(self, in_=False):
        if self._global_capturing is not None:
            self._global_capturing.suspend_capturing(in_=in_)

    def suspend(self, in_=False):
        # Need to undo local capsys-et-al if it exists before disabling global capture.
        self.suspend_fixture()
        self.suspend_global_capture(in_)

    def resume(self):
        self.resume_global_capture()
        self.resume_fixture()

    def read_global_capture(self):
        return self._global_capturing.readouterr()

    # Fixture Control

    def set_fixture(self, capture_fixture: "CaptureFixture") -> None:
        if self._capture_fixture:
            current_fixture = self._capture_fixture.request.fixturename
            requested_fixture = capture_fixture.request.fixturename
            capture_fixture.request.raiseerror(
                "cannot use {} and {} at the same time".format(
                    requested_fixture, current_fixture
                )
            )
        self._capture_fixture = capture_fixture

    def unset_fixture(self) -> None:
        self._capture_fixture = None

    def activate_fixture(self):
        """If the current item is using ``capsys`` or ``capfd``, activate them so they take precedence over
        the global capture.
        """
        if self._capture_fixture:
            self._capture_fixture._start()

    def deactivate_fixture(self):
        """Deactivates the ``capsys`` or ``capfd`` fixture of this item, if any."""
        if self._capture_fixture:
            self._capture_fixture.close()

    def suspend_fixture(self, pop_to_orig=False):
        if self._capture_fixture:
            self._capture_fixture._suspend(pop_to_orig=pop_to_orig)

    def resume_fixture(self):
        if self._capture_fixture:
            self._capture_fixture._resume()

    # Helper context managers

    @contextlib.contextmanager
    def global_and_fixture_disabled(self):
        """Context manager to temporarily disable global and current fixture capturing."""
        self.suspend()
        try:
            yield
        finally:
            self.resume()

    @contextlib.contextmanager
    def item_capture(self, when, item):
        self.resume_global_capture()
        self.resume_fixture()
        try:
            yield
        finally:
            self.suspend_fixture(pop_to_orig=True)
            self.suspend_global_capture(in_=False)

        out, err = self.read_global_capture()
        item.add_report_section(when, "stdout", out)
        item.add_report_section(when, "stderr", err)

    # Hooks

    @pytest.hookimpl(hookwrapper=True)
    def pytest_make_collect_report(self, collector):
        if isinstance(collector, pytest.File):
            self.resume_global_capture()
            outcome = yield
            self.suspend_global_capture()
            out, err = self.read_global_capture()
            rep = outcome.get_result()
            if out:
                rep.sections.append(("Captured stdout", out))
            if err:
                rep.sections.append(("Captured stderr", err))
        else:
            yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        with self.item_capture("setup", item):
            # If a capture fixture is requested, self._capture_fixture
            # will be created and started during the yield.
            yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        with self.item_capture("call", item):
            yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item):
        with self.item_capture("teardown", item):
            yield
            # If a capture fixture was requested, self._capture_fixture
            # was stopped and destroyed during the yield.

    @pytest.hookimpl(tryfirst=True)
    def pytest_keyboard_interrupt(self, excinfo):
        self.deactivate_fixture()
        self.stop_global_capturing()

    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(self, excinfo):
        self.deactivate_fixture()
        self.stop_global_capturing()


class CaptureFixture:
    """
    Object returned by :py:func:`capsys`, :py:func:`capsysbinary`, :py:func:`capfd` and :py:func:`capfdbinary`
    fixtures.
    """

    def __init__(self, captureclass, request):
        self.request = request
        self._capture = MultiCapture(
            in_=None, out=captureclass(1), err=captureclass(2),
        )
        self._captured_out = captureclass.EMPTY_BUFFER
        self._captured_err = captureclass.EMPTY_BUFFER

    def _start(self):
        self._capture.start_capturing()

    def close(self):
        out, err = self._capture.pop_outerr_to_orig()
        self._captured_out += out
        self._captured_err += err
        self._capture.stop_capturing()

    def readouterr(self):
        """Read and return the captured output so far, resetting the internal buffer.

        :return: captured content as a namedtuple with ``out`` and ``err`` string attributes
        """
        captured_out, captured_err = self._captured_out, self._captured_err
        out, err = self._capture.readouterr()
        captured_out += out
        captured_err += err
        self._captured_out = self._capture.out.EMPTY_BUFFER
        self._captured_err = self._capture.err.EMPTY_BUFFER
        return CaptureResult(captured_out, captured_err)

    def _suspend(self, *, pop_to_orig: bool = False) -> None:
        """Suspends this fixture's own capturing temporarily."""
        if pop_to_orig:
            out, err = self._capture.pop_outerr_to_orig()
            self._captured_out += out
            self._captured_err += err
        self._capture.suspend_capturing()

    def _resume(self):
        """Resumes this fixture's own capturing temporarily."""
        self._capture.resume_capturing()

    @contextlib.contextmanager
    def disabled(self):
        """Temporarily disables capture while inside the 'with' block."""
        capmanager = self.request.config.pluginmanager.getplugin("capturemanager")
        with capmanager.global_and_fixture_disabled():
            yield


# The fixtures.


@pytest.fixture
def capsys(request):
    """Enable text capturing of writes to ``sys.stdout`` and ``sys.stderr``.

    The captured output is made available via ``capsys.readouterr()`` method
    calls, which return a ``(out, err)`` namedtuple.
    ``out`` and ``err`` will be ``text`` objects.
    """
    capman = request.config.pluginmanager.getplugin("capturemanager")
    capture_fixture = CaptureFixture(SysCapture, request)
    capman.set_fixture(capture_fixture)
    capture_fixture._start()
    yield capture_fixture
    capture_fixture.close()
    capman.unset_fixture()


@pytest.fixture
def capsysbinary(request):
    """Enable bytes capturing of writes to ``sys.stdout`` and ``sys.stderr``.

    The captured output is made available via ``capsysbinary.readouterr()``
    method calls, which return a ``(out, err)`` namedtuple.
    ``out`` and ``err`` will be ``bytes`` objects.
    """
    capman = request.config.pluginmanager.getplugin("capturemanager")
    capture_fixture = CaptureFixture(SysCaptureBinary, request)
    capman.set_fixture(capture_fixture)
    capture_fixture._start()
    yield capture_fixture
    capture_fixture.close()
    capman.unset_fixture()


@pytest.fixture
def capfd(request):
    """Enable text capturing of writes to file descriptors ``1`` and ``2``.

    The captured output is made available via ``capfd.readouterr()`` method
    calls, which return a ``(out, err)`` namedtuple.
    ``out`` and ``err`` will be ``text`` objects.
    """
    capman = request.config.pluginmanager.getplugin("capturemanager")
    capture_fixture = CaptureFixture(FDCapture, request)
    capman.set_fixture(capture_fixture)
    capture_fixture._start()
    yield capture_fixture
    capture_fixture.close()
    capman.unset_fixture()


@pytest.fixture
def capfdbinary(request):
    """Enable bytes capturing of writes to file descriptors ``1`` and ``2``.

    The captured output is made available via ``capfd.readouterr()`` method
    calls, which return a ``(out, err)`` namedtuple.
    ``out`` and ``err`` will be ``byte`` objects.
    """
    capman = request.config.pluginmanager.getplugin("capturemanager")
    capture_fixture = CaptureFixture(FDCaptureBinary, request)
    capman.set_fixture(capture_fixture)
    capture_fixture._start()
    yield capture_fixture
    capture_fixture.close()
    capman.unset_fixture()
