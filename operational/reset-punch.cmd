@echo off
REM ============================================================================
REM  reset-punch.cmd  --  Punch Analytics shim deterministic reset
REM
REM  Use this when a laptop's Claude Desktop install fails authentication
REM  against the Punch Analytics backend after a fresh MCPB install. The
REM  shim's auto-detection heuristics (v3.0.5 v1-nuke, v3.0.7 seed-time
REM  real-key gate) catch the common cases on FRESH installs; this script
REM  is the universal "kick it back to factory" recovery for installs that
REM  are already in a bad state.
REM
REM  What it does, in order:
REM    1. Stops Claude Desktop (both the MS Store and Win32 builds).
REM    2. Deletes every backends.json the shim might pick up on next
REM       launch -- both un-sandboxed (%%APPDATA%%, %%LOCALAPPDATA%%) and
REM       MS-Store-sandboxed (Packages\Claude_pzs8sxrjxfjjc\...).
REM    3. Deletes the shim's local log file so the next launch starts
REM       clean (handy when an operator captures logs to send back).
REM    4. Prints next-step instructions for reinstalling the MCPB
REM       extension with the API Key field LEFT BLANK (Kerberos mode).
REM
REM  Safe to run multiple times. Missing files / processes are ignored.
REM  Does NOT touch Claude Desktop's own user_config -- that gets cleared
REM  naturally when the user uninstalls + reinstalls the MCPB extension.
REM ============================================================================

setlocal enableextensions

echo.
echo === Punch Analytics reset (deterministic) ===
echo.

echo [1/4] Stopping Claude Desktop ...
taskkill /F /IM "Claude.exe"        >nul 2>&1
taskkill /F /IM "claude.exe"        >nul 2>&1
taskkill /F /IM "ClaudeHelper.exe"  >nul 2>&1
echo       (any "not found" messages above are fine)

echo.
echo [2/4] Deleting backends.json from every known location ...

REM -- un-sandboxed Win32 paths --
if defined APPDATA       call :nuke "%APPDATA%\PunchAnalytics\backends.json"
if defined LOCALAPPDATA  call :nuke "%LOCALAPPDATA%\PunchAnalytics\backends.json"

REM -- MS Store Claude sandbox redirects APPDATA/LOCALAPPDATA into this prefix.
REM    See auto-memory: msstore-claude-sandbox-appdata.md
if defined LOCALAPPDATA (
    call :nuke "%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\PunchAnalytics\backends.json"
    call :nuke "%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\PunchAnalytics\backends.json"
)

echo.
echo [3/4] Deleting shim log file ...
if defined LOCALAPPDATA  call :nuke "%LOCALAPPDATA%\PunchAnalytics\shim.log"
if defined LOCALAPPDATA  call :nuke "%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\PunchAnalytics\shim.log"

echo.
echo [4/4] Reset complete. Next steps:
echo.
echo       1. Open Claude Desktop.
echo       2. Settings -^> Extensions -^> remove "Punch Analytics" if present.
echo       3. Reinstall the latest Punch Analytics .mcpb from:
echo            https://github.com/Zenotech-bv/punch-analytics-shim/releases
echo       4. When the install dialog asks for "API Key":
echo            LEAVE IT BLANK.
echo          The shim will use your Windows logon (Kerberos) by default.
echo       5. If your account is a SERVICE account (not Kerberos), paste
echo          the real key issued by the Punch Analytics admin instead.
echo.

endlocal
goto :eof

:nuke
REM Single-arg helper: silently delete the given path if it exists, log either way.
if exist %~1 (
    del /F /Q %~1 >nul 2>&1
    if exist %~1 (
        echo       FAILED to delete: %~1
    ) else (
        echo       deleted:          %~1
    )
) else (
    echo       not present:      %~1
)
goto :eof
