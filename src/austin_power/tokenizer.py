from __future__ import annotations

import atexit
import json
import queue
import re
import subprocess
import sys
import threading
from functools import lru_cache
from importlib.metadata import version

import apsw
import apsw.fts5

RULES_VERSION = 1
ASCII_RUN = re.compile(r"[A-Za-z0-9_]+(?:[./-][A-Za-z0-9_]+)*")
_SEP = re.compile(r"[_./-]+")
_KEEP = ("NN", "VV", "VA", "XR", "SH", "SL", "SN")

REQUEST_TIMEOUT = 30.0
KILL_GRACE = 2.0


class TokenizerMismatchError(RuntimeError):
    """Raised (and re-exported by db.py, which needs it too) when the worker's
    reported signature() doesn't match ours, or the index was rebuilt by a
    different austin-power version. Defined here rather than in db.py so
    tokenizer itself can raise it without importing db (which imports
    tokenizer) and creating a cycle.
    """


class WorkerError(RuntimeError):
    """The kiwi worker backend failed after its one retry, or reported a
    genuine tokenize() error. Distinct from TokenizerMismatchError so callers
    (server.py's storage-error mapping) can catch both without catching
    unrelated RuntimeErrors."""


class _WorkerFailure(Exception):
    """Internal control-flow only: a communication failure (EOF/garbage/
    timeout) that should trigger kill+respawn+retry-once, never escapes
    WorkerBackend's public methods."""


@lru_cache(maxsize=1)
def get_kiwi():
    from kiwipiepy import Kiwi
    return Kiwi()


def signature() -> str:
    return f"kiwi/{RULES_VERSION}/kiwipiepy-{version('kiwipiepy')}/model-{version('kiwipiepy_model')}"


class _InprocBackend:
    """Current process runs Kiwi directly. Used by import, the post-compact
    hook fallback, and tests (all short-lived processes, so memory is
    reclaimed on exit — see spec §2.5.1)."""

    def ensure_ready(self) -> None:
        get_kiwi()

    def tokenize(self, text: str) -> list[tuple[str, str, int, int]]:
        return [(t.form, t.tag, t.start, t.len) for t in get_kiwi().tokenize(text)]

    def shutdown(self) -> None:
        pass


class _ProcHandle:
    """A spawned worker process plus a background thread pumping its stdout
    into a queue, so readers can apply a timeout without leaking a fresh
    thread per read."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.lines: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:
                self.lines.put(line)
        except Exception:  # noqa: BLE001, S110 - pipe went away; the finally below still signals EOF
            pass
        finally:
            self.lines.put("")  # EOF sentinel: readline() must return "" promptly, not wait out a timeout

    def readline(self, timeout: float) -> str | None:
        """One line (without trailing newline), "" on EOF, None on timeout."""
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            return None
        return line.rstrip("\n")


class _WorkerBackend:
    """Server-side backend: spawns `python -m austin_power.kiwi_worker` lazily
    and talks newline-JSON over its stdio (spec §2.5.1)."""

    def __init__(self, idle_seconds: int = 600):
        self.idle_seconds = idle_seconds
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._handle: _ProcHandle | None = None
        self._idle_timer: threading.Timer | None = None
        self._idle_gen = 0

    # -- process lifecycle (caller holds self._lock) -------------------

    def _spawn(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "austin_power.kiwi_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        handle = _ProcHandle(proc)
        line = handle.readline(REQUEST_TIMEOUT)
        if not line:
            self._kill(proc)
            raise _WorkerFailure("kiwi worker exited before reporting ready")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            self._kill(proc)
            raise _WorkerFailure(f"kiwi worker sent a non-JSON ready line: {e}") from e
        if not msg.get("ready"):
            self._kill(proc)
            raise _WorkerFailure("kiwi worker did not report ready")
        got_sig = msg.get("signature")
        if got_sig != signature():
            self._kill(proc)
            raise TokenizerMismatchError(
                f"kiwi worker signature mismatch: worker={got_sig!r} parent={signature()!r}"
            )
        self._proc, self._handle = proc, handle

    def _ensure_spawned(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = self._handle = None
        self._spawn()

    @staticmethod
    def _kill(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001, S110 - best-effort reap
            pass

    def _request(self, text: str) -> list[tuple[str, str, int, int]]:
        assert self._proc is not None and self._handle is not None
        try:
            self._proc.stdin.write(json.dumps({"text": text}, ensure_ascii=True) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise _WorkerFailure(f"write to kiwi worker failed: {e}") from e
        line = self._handle.readline(REQUEST_TIMEOUT)
        if not line:
            raise _WorkerFailure("kiwi worker EOF or timeout")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            raise _WorkerFailure(f"kiwi worker sent a non-JSON response: {e}") from e
        if "error" in msg:
            raise WorkerError(f"kiwi worker error: {msg['error']}")
        toks = msg.get("tokens")
        if toks is None:
            raise _WorkerFailure("kiwi worker response missing 'tokens'")
        return [(t[0], t[1], t[2], t[3]) for t in toks]

    # -- idle timer (caller holds self._lock) --------------------------

    def _cancel_idle(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        self._idle_gen += 1

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self.idle_seconds and self.idle_seconds > 0:
            gen = self._idle_gen
            t = threading.Timer(self.idle_seconds, self._on_idle, args=(gen,))
            t.daemon = True
            self._idle_timer = t
            t.start()

    def _on_idle(self, gen: int) -> None:
        with self._lock:
            if gen != self._idle_gen or self._proc is None:
                return  # a request raced this timer and already reset it
            proc = self._proc
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=KILL_GRACE)
            except Exception:  # noqa: BLE001
                self._kill(proc)
            if self._proc is proc:
                self._proc = self._handle = None

    # -- public API -----------------------------------------------------

    def ensure_ready(self) -> None:
        with self._lock:
            self._cancel_idle()
            try:
                self._ensure_spawned()
            except _WorkerFailure:
                self._proc = self._handle = None
                try:
                    self._ensure_spawned()
                except _WorkerFailure as e:
                    raise WorkerError(f"kiwi worker failed to start after retry: {e}") from e
            self._arm_idle()

    def tokenize(self, text: str) -> list[tuple[str, str, int, int]]:
        with self._lock:
            self._cancel_idle()
            last_exc: Exception | None = None
            for _attempt in range(2):
                try:
                    self._ensure_spawned()
                    result = self._request(text)
                except _WorkerFailure as e:
                    last_exc = e
                    self._kill(self._proc)
                    self._proc = self._handle = None
                    continue
                self._arm_idle()
                return result
            self._arm_idle()
            raise WorkerError(f"kiwi worker failed after retry: {last_exc}") from last_exc

    def shutdown(self) -> None:
        with self._lock:
            self._cancel_idle()
            proc = self._proc
            self._proc = self._handle = None
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=KILL_GRACE)
            except Exception:  # noqa: BLE001
                self._kill(proc)


_backend_lock = threading.RLock()
_backend: _InprocBackend | _WorkerBackend = _InprocBackend()


def set_backend(name: str, *, idle_seconds: int = 600) -> None:
    global _backend
    with _backend_lock:
        old = _backend
        if name == "inproc":
            _backend = _InprocBackend()
        elif name == "worker":
            _backend = _WorkerBackend(idle_seconds=idle_seconds)
        else:
            raise ValueError(f"unknown tokenizer backend: {name!r}")
        if old is not _backend:
            old.shutdown()


def ensure_ready() -> None:
    with _backend_lock:
        backend = _backend
    backend.ensure_ready()


def shutdown() -> None:
    with _backend_lock:
        _backend.shutdown()


atexit.register(shutdown)


def analyze(text: str, *, for_query: bool) -> list[tuple[int, int, tuple[str, ...]]]:
    out: list[tuple[int, int, tuple[str, ...]]] = []
    runs = [(m.start(), m.end()) for m in ASCII_RUN.finditer(text)]
    for s, e in runs:
        word = text[s:e].lower()
        toks = [word]
        if not for_query and _SEP.search(word):
            toks += [p for p in _SEP.split(word) if p and p != word]
        out.append((s, e, tuple(dict.fromkeys(toks))))
    if any(ord(ch) > 127 for ch in text):
        with _backend_lock:
            backend = _backend
        for form, tag, start, length in backend.tokenize(text):
            s, e = start, start + length
            if e <= s or any(s < re_ and e > rs for rs, re_ in runs):
                continue
            if tag.startswith(_KEEP) and form.strip():
                out.append((s, e, (form.lower(),)))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def query_tokens(text: str) -> list[str]:
    return [toks[0] for _, _, toks in analyze(text, for_query=True)]


@apsw.fts5.StringTokenizer
def _kiwi_tokenizer(con, args):
    def tokenize(text: str, flags: int, locale):
        for s, e, toks in analyze(text, for_query=bool(flags & apsw.FTS5_TOKENIZE_QUERY)):
            yield (s, e, *toks)
    return tokenize

def register(conn: apsw.Connection) -> None:
    conn.register_fts5_tokenizer("kiwi", _kiwi_tokenizer)
