"""v3.5.1 regression guard: the auto-update must NOT re-exec in-session.

os.execv on Windows spawns a NEW process (dropping the stdio pipe Claude
Desktop launched us with) and mangles the space in the "Claude Extensions"
install path — python then receives a truncated path and exits "early", so
the update-pulling launch shows the server disconnected. The fix stages the
new bytes to disk and lets the NEXT natural launch pick them up.

This is a STATIC source check on purpose: exercising _maybe_self_update
behaviorally requires monkeypatching module globals (__file__, _AUTO_UPDATE,
_fetch_update_source), which leaks into the shim's shared-module reload tests.
A source-level invariant catches the only regression that matters — someone
re-introducing the re-exec — without touching global state.
"""
import importlib
import inspect


def test_self_update_stages_and_never_reexecs():
    # Import INSIDE the test (not at module level) so it happens after the
    # conftest session fixture has set the isolated env — a module-level
    # import runs at collection time and corrupts the shared _BACKENDS.
    shim_server = importlib.import_module("shim_server")
    src = inspect.getsource(shim_server._maybe_self_update)
    # match the CALL, not the word (the explanatory comment mentions os.execv)
    assert "os.execv(" not in src, (
        "auto-update re-exec reintroduced — os.execv breaks the Claude Desktop "
        "stdio pipe on Windows and mangles the spaced install path"
    )
    assert "auto_update_staged" in src, (
        "auto-update should stage the download and apply on next launch"
    )
