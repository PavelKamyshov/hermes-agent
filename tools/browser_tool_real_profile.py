"""Real-profile local browsing: snapshot the user's default Chromium profile into a
hermes-owned copy, launch the real browser binary on it, and attach agent-browser.

State (``_REAL_PROFILE_SESSION``, ``_real_profile_cdp_lock``, ``_real_profile_cdp_cache``,
``_real_profile_chrome_procs``) lives in ``tools.browser_tool``; it is read
through ``_bt`` (resolved per call — never import ``tools.browser_tool`` at import time).
"""

import os
import re
import shutil
import subprocess
import sys
import time
from typing import Optional, Tuple
from agent.proxy_bypass import loopback_request_kwargs
from tools.browser_tool_origin import origin_module as _origin
from tools import browser_tool_cloud as _cloud
from tools import browser_tool_install as _install
from tools import browser_tool_lightpanda_fallback as _lp
from tools import browser_tool_session as _session

_RP = "browser.use_real_profile is on, but "


def _terminate_real_profile_chrome() -> None:
    """Terminate real-browser processes launched for real-profile sessions (idempotent, atexit-safe);
    agent-browser only ATTACHED to them, so its own session cleanup never kills them."""
    from tools.browser_lightpanda import _terminate
    _bt = _origin()
    while _bt._real_profile_chrome_procs:
        _terminate(_bt._real_profile_chrome_procs.pop(), what="real-profile chrome")


_real_profile_last_used = 0.0


def _mark_real_profile_used() -> None:
    """LOCAL PATCH (Sky, 2026-09-27, not upstream): note that the copy browser was just used."""
    global _real_profile_last_used
    _real_profile_last_used = time.time()


def _release_idle_real_profile_chrome(idle_seconds: float | None = None) -> bool:
    """LOCAL PATCH (Sky, 2026-09-27, not upstream): stop the copy Chrome once the agent is idle.

    The launched instance is a full, invisible Chrome on the copy profile. While it runs, macOS
    routes the user's own Chrome launches to it (single-instance app), so their browser appears
    dead; it also holds ~1.2 GB. agent-browser only ATTACHED to it, so nothing else ever stops it
    except our process exit. Called from the idle-session reaper; returns True when it released one.
    Also sweeps a surviving instance from an earlier Hermes process (same copy dir = ours).
    """
    _bt = _origin()
    idle = float(_bt.BROWSER_SESSION_INACTIVITY_TIMEOUT) if idle_seconds is None else float(idle_seconds)
    if time.time() - _real_profile_last_used < idle:
        return False
    released = False
    if _bt._real_profile_chrome_procs:
        _terminate_real_profile_chrome()
        released = True
    copy_dir = None
    try:
        from hermes_cli.browser_connect import real_profile_copy_dir
        from hermes_cli.browser_connect import detect_default_chromium
        browser = detect_default_chromium()
        if browser:
            copy_dir = real_profile_copy_dir(browser)
    except Exception as exc:  # pragma: no cover - defensive, never break the reaper
        _bt.logger.debug("real-profile idle release: copy dir unresolved (%s)", exc)
    if copy_dir:
        import subprocess as _sp
        try:
            found = _sp.run(["pgrep", "-f", f"user-data-dir={copy_dir}"], capture_output=True, text=True,
                            timeout=5).stdout.split()
            for pid in found:
                try:
                    os.kill(int(pid), 15)
                    released = True
                except (OSError, ValueError):
                    pass
        except (OSError, _sp.SubprocessError) as exc:
            _bt.logger.debug("real-profile idle release: sweep failed (%s)", exc)
    if released:
        _agent_browser_close_session(_bt._REAL_PROFILE_SESSION)
        _bt._real_profile_cdp_cache.pop("cdp", None)
        _bt.logger.info("real-profile: released idle copy Chrome (the user's own browser can open again)")
    return released


def _cdp_http_ready(http_cdp: str) -> bool:
    """True when an ``http://host:port`` CDP discovery root answers."""
    from tools.browser_lightpanda import _cdp_ready
    return _cdp_ready(http_cdp, timeout=1.0)


def _real_profile_daemon_env() -> tuple:
    """Reaper-visible socket dir + ``owner_pid`` claim like every other lane (agent-browser's
    default dir is invisible to the reaper — #100855). The daemon-side idle timeout is dropped:
    Chrome is launched by Hermes, not the daemon, so a self-exiting daemon would leave Chrome
    holding the copy dir under the next snapshot overlay. Returns ``(env, socket_dir)``."""
    _bt = _origin()
    socket_dir = _session._prepare_session_socket_dir(_bt._REAL_PROFILE_SESSION)
    env = _session._agent_browser_command_env(socket_dir)
    env.pop("AGENT_BROWSER_IDLE_TIMEOUT_MS", None)
    return env, socket_dir


def _capture_agent_browser_cli(argv: list, timeout: float, tag: str) -> subprocess.CompletedProcess:
    """Run an agent-browser CLI argv once, capturing output through temp files (not pipes).

    The CLI forks a resident daemon that inherits its stdio and outlives the CLI, so with
    pipe capture the CLI can exit while its output never reaches EOF: POSIX burns the whole
    timeout draining pipes on every call, and Windows' ``TimeoutExpired`` cleanup re-drains
    with no timeout, blocking past the outer tool deadline (#96731). Same pattern as
    ``_popen_agent_browser``: wait for the CLI alone — a grandchild holding the inherited
    file handles is harmless. Raises ``TimeoutExpired`` after killing the CLI."""
    env, socket_dir = _real_profile_daemon_env()
    stdout_path = os.path.join(socket_dir, f"_stdout_{tag}")
    stderr_path = os.path.join(socket_dir, f"_stderr_{tag}")
    proc = _session._popen_agent_browser(argv, env, socket_dir, tag)
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    stdout, stderr = _session._read_command_output_files(stdout_path, stderr_path)
    _session._unlink_command_output_files(stdout_path, stderr_path)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout)
    return subprocess.CompletedProcess(argv, proc.returncode, stdout=stdout, stderr=stderr)


def _agent_browser_session_cmd(session_name: str, *cmd: str, log_label: str) -> Optional[subprocess.CompletedProcess]:
    """Run ``agent-browser --session <name> <cmd...>``; None when agent-browser is missing or the run fails."""
    _bt = _origin()
    try:
        browser_cmd = _install._find_agent_browser()
    except FileNotFoundError:
        return None
    try:
        return _capture_agent_browser_cli(
            [*_session._agent_browser_argv(browser_cmd), "--session", session_name, *cmd],
            timeout=15, tag=f"rp-{log_label.replace(' ', '-')}",
        )
    except (subprocess.SubprocessError, OSError) as e:
        _bt.logger.debug("real-profile %s failed: %s", log_label, e)
        return None


def _agent_browser_get_cdp(session_name: str) -> Optional[str]:
    """HTTP CDP discovery root of an agent-browser session (from its ``ws://`` cdp-url), or None."""
    proc = _agent_browser_session_cmd(session_name, "get", "cdp-url", log_label="get cdp-url")
    m = re.search(r"ws://127\.0\.0\.1:(\d+)/", (proc.stdout or "").strip()) if proc is not None else None
    return f"http://127.0.0.1:{m.group(1)}" if m else None


def _read_devtools_port(data_dir: str) -> Optional[str]:
    """First line of Chrome's ``DevToolsActivePort`` in ``data_dir`` (None when unreadable)."""
    try:
        with open(os.path.join(data_dir, "DevToolsActivePort"), encoding="utf-8-sig") as fh:
            return fh.readline().strip()
    except OSError:
        return None


def _surviving_chrome_cdp(data_dir: str) -> Optional[str]:
    """HTTP CDP root of a Chrome still running on ``data_dir``, or None. ``DevToolsActivePort``
    outlives a crashed Chrome and its port can be recycled by another local CDP server, so the
    file's browser id (line 2) must match what ``/json/version`` reports before it is trusted."""
    try:
        with open(os.path.join(data_dir, "DevToolsActivePort"), encoding="utf-8-sig") as fh:
            port, browser_path = fh.readline().strip(), fh.readline().strip()
    except OSError:
        return None
    if not port.isdigit() or not browser_path.startswith("/devtools/browser/"):
        return None
    http_cdp = f"http://127.0.0.1:{port}"
    try:
        import requests
        ws_url = str(requests.get(f"{http_cdp}/json/version", timeout=2, **loopback_request_kwargs(http_cdp))
                     .json().get("webSocketDebuggerUrl") or "")
    except Exception:
        return None
    return http_cdp if ws_url.endswith(browser_path) else None


def _cdp_on_data_dir(http_cdp: str, data_dir: str) -> bool:
    """True when the CDP endpoint's browser runs on ``data_dir`` (DevToolsActivePort match proves it
    is our profile copy, not a throwaway temp dir a raced/stale launch fell back to)."""
    m = re.search(r":(\d+)", http_cdp or "")
    return bool(m) and _read_devtools_port(data_dir) == m.group(1)


def _agent_browser_close_session(session_name: str) -> None:
    """Best-effort close of an agent-browser session (stale/wrong-dir cleanup)."""
    _agent_browser_session_cmd(session_name, "close", log_label="session close")


_REAL_PROFILE_CHROME_FLAGS = (
    "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
    "--disable-hang-monitor", "--disable-popup-blocking", "--disable-prompt-on-repost",
    "--disable-sync", "--disable-features=Translate", "--no-startup-window",
)


def _clear_copy_session_restore(copy_dir: str) -> None:
    """LOCAL PATCH (Sky, 2026-09-27, not upstream): drop the copy's saved tabs before launch.

    The snapshot mirrors the user's session-restore files next to their cookies, so Chrome would
    restore their whole open tab set inside the agent's copy (dozens of heavy pages), which stalls
    the first agent-browser ``open`` past its 120 s timeout. Logins are unaffected - Cookies and
    Login Data stay exactly as snapshotted. Re-applied by
    ~/.hermes/scripts/real-profile-clean-session-patch.py (see the hermes-local-patches skill).
    """
    try:
        entries = os.listdir(copy_dir)
    except OSError:
        return
    profile_names = ["Default", *(e for e in entries if e.startswith("Profile "))]
    for name in profile_names:
        profile_dir = os.path.join(copy_dir, name)
        for target in ("Sessions", "Current Session", "Current Tabs", "Last Session", "Last Tabs"):
            path = os.path.join(profile_dir, target)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass


def _real_profile_unsupported_reason(browser) -> Optional[str]:
    """Fail-closed message when the default browser can't be used, else None.

    A pre-release channel lives in a profile dir we don't resolve; normalizing to the stable
    family would drive a DIFFERENT profile/account (wrong-principal bug), so refuse rather than guess.
    """
    from hermes_cli.browser_connect import UNSUPPORTED_CHANNEL
    if browser is None:
        return (_RP + "your default browser is not a supported Chromium browser (Chrome, Edge, Brave, "
                "Brave Origin, Chromium). Real-profile browsing requires a Chromium default; set one or turn the toggle off.")
    if browser == UNSUPPORTED_CHANNEL:
        return (_RP + "your default browser is a pre-release Chromium channel (Beta / Dev / Canary), which "
                "real-profile browsing does not support. Set your default to a "
                "stable Chrome / Edge / Brave / Brave Origin / Chromium, or turn the toggle off.")
    return None


def _real_profile_snapshot_error(err: str) -> str:
    """User-facing message for a failed profile snapshot; a locked profile adds the approved-close
    command, which the agent must ASK the user about first (it quits their browser)."""
    from hermes_cli.browser_connect import _PROFILE_LOCKED_PREFIX
    if err and err.startswith(_PROFILE_LOCKED_PREFIX):
        return (err[len(_PROFILE_LOCKED_PREFIX):] + " To close it (only after the user approves — it "
                "quits their browser and loses unsaved tabs), run: `hermes browser close-profile`, then retry.")
    return f"{_RP}{err}"


def _sweep_copy_dir_processes(copy_dir: str) -> int:
    """LOCAL PATCH (Sky, 2026-09-27, not upstream): SIGTERM every process on the copy profile.

    A Chrome left on the copy dir by an earlier run (a headed CDP session, a manual launch) owns the
    profile singleton, so a fresh instance exits at once and real-profile browsing is dead with
    "Chrome exited during startup". Everything matching the copy dir is ours by construction.
    Returns how many processes were signalled.
    """
    killed = 0
    try:
        out = subprocess.run(["pgrep", "-f", f"user-data-dir={copy_dir}"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    for pid in out.split():
        try:
            os.kill(int(pid), 15)
            killed += 1
        except (OSError, ValueError):
            pass
    return killed


REAL_PROFILE_STALE_FALLBACK_SECONDS = 12 * 3600  # local patch: how stale a copy may be and still be used


def _fresh_copy_dir(browser: str) -> Optional[str]:
    """LOCAL PATCH (Sky, 2026-09-27, not upstream): the existing copy dir when it is still fresh.

    "Fresh" = its Cookies database was written within REAL_PROFILE_STALE_FALLBACK_SECONDS. A stale
    or missing copy returns None, so the caller keeps the original fail-closed behaviour.
    """
    try:
        from hermes_cli.browser_connect import real_profile_copy_dir
        copy_dir = real_profile_copy_dir(browser)
        for name in ("Default", *(f"Profile {i}" for i in range(1, 10))):
            cookies = os.path.join(copy_dir, name, "Cookies")
            if os.path.exists(cookies) and (time.time() - os.path.getmtime(cookies)) < REAL_PROFILE_STALE_FALLBACK_SECONDS:
                return copy_dir
    except Exception:
        return None
    return None


def _launch_real_profile_chrome(real_binary: str, copy_dir: str) -> Tuple[Optional[int], Optional[str]]:
    """Launch the user's REAL browser binary on the profile COPY; return (debug_port, error).

    agent-browser's own launch force-adds --use-mock-keychain / --password-store=basic, which makes
    macOS Chrome drop every keychain-encrypted cookie (signed-out copy); launching the real binary
    ourselves keeps the OS keychain path intact and agent-browser attaches via ``--cdp <port>``.
    Headless by default (a focus-stealing window defeats a background capability); Chrome's NEW
    headless shares the profile's cookie store (legacy --headless does not). browser.headed /
    AGENT_BROWSER_HEADED opts into a window, except on a display-less Linux host (launch would die).
    """
    _bt = _origin()
    try:
        os.unlink(os.path.join(copy_dir, "DevToolsActivePort"))  # stale port confuses reuse probes
    except OSError:
        pass
    _clear_copy_session_restore(copy_dir)  # local patch: the copy opens clean, logins intact
    chrome_argv = [real_binary, f"--user-data-dir={copy_dir}", *_REAL_PROFILE_CHROME_FLAGS]
    last_error = _RP + "the real-profile browser did not expose a debug port in time. Retry, or turn the toggle off."
    # LOCAL PATCH (Sky, 2026-09-27, not upstream): a Chrome left on the copy dir by an earlier run
    # owns the profile singleton, so our fresh instance exits instantly and browsing is dead for
    # good ("Chrome exited during startup"). Sweep it and retry once before reporting failure.
    for attempt in (1, 2):
        _session._ensure_screen_for_headed_chromium()
        browser_env = _bt._build_browser_env()  # carries the Bot Desktop DISPLAY when one is running
        _has_display = bool(browser_env.get("DISPLAY") or browser_env.get("WAYLAND_DISPLAY"))
        if not (_cloud._is_headed_mode() and (_has_display or not sys.platform.startswith("linux"))):
            if "--headless=new" not in chrome_argv:  # the retry pass must not append it twice
                chrome_argv.append("--headless=new")
        try:
            chrome_proc = subprocess.Popen(chrome_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           stdin=subprocess.DEVNULL, start_new_session=True, env=browser_env)
        except (subprocess.SubprocessError, OSError) as e:
            return None, f"{_RP}the launch failed: {e}"
        _bt._real_profile_chrome_procs.append(chrome_proc)

        exited = False
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            line = _read_devtools_port(copy_dir) or ""
            if line.isdigit():
                return int(line), None
            if chrome_proc.poll() is not None:
                exited = True
                break
            time.sleep(0.25)
        _terminate_real_profile_chrome()
        if exited:
            last_error = _RP + "Chrome exited during startup (another instance may hold the profile copy)."
        if attempt == 1:
            _bt.logger.info("real-profile: swept %d leftover process(es) on the copy dir; retrying the launch",
                            _sweep_copy_dir_processes(copy_dir))
            time.sleep(2)
    return None, last_error


def _attach_agent_browser_to_real_profile(port: int, copy_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """Make agent-browser ATTACH to the running Chrome (never launch its own); returns ``(http_cdp, error)``.

    The daemon may answer with the endpoint of a browser IT spawned (throwaway temp profile);
    the DevToolsActivePort OUR Chrome wrote is authoritative on disagreement.
    """
    _bt = _origin()
    try:
        browser_cmd = _install._find_agent_browser()
    except FileNotFoundError as e:
        return None, f"{_RP}the local browser engine (agent-browser) is not installed: {e}"
    argv = [*_session._agent_browser_argv(browser_cmd), "--session", _bt._REAL_PROFILE_SESSION,
            "--cdp", str(port), "open", "about:blank"]
    try:
        proc = _capture_agent_browser_cli(
            argv, timeout=_bt._get_open_command_timeout(first_open=True), tag="rp-open",
        )
    except subprocess.TimeoutExpired:
        return None, _RP + "the real-profile browser took too long to start. Retry, or turn the toggle off."
    except (subprocess.SubprocessError, OSError) as e:
        return None, f"{_RP}the launch failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, f"{_RP}the real-profile browser failed to start: {tail[-1] if tail else f'exit {proc.returncode}'}"
    cdp = _agent_browser_get_cdp(_bt._REAL_PROFILE_SESSION)
    our_port = _read_devtools_port(copy_dir)
    if our_port is not None and (m := re.search(r":(\d+)", cdp or "")) and m.group(1) != our_port:
        cdp = f"http://127.0.0.1:{our_port}"
    if not cdp:
        return None, _RP + "the real-profile browser started without exposing a devtools endpoint. Retry, or turn the toggle off."
    return cdp, None


def _real_profile_cdp() -> tuple:
    """Resolve ``(cdp_url, error)`` for consented real-profile browsing.

    Snapshot -> launch real binary on the copy -> return its HTTP CDP endpoint. The copy is a
    non-default dir, so it sidesteps the Chrome >=136 default-profile remote-debugging block and
    never contends with the user's running browser. One shared agent-browser session is reused
    across calls (cached, re-validated). ``(None, message)`` fail-closed; ``(None, None)`` when consent is off.
    """
    _bt = _origin()
    if not _cloud._use_real_profile():
        # Consent is off: delete any snapshot store (copies of cookies/logins) so
        # revoking consent actually removes the credential copies.
        try:
            from hermes_cli.browser_connect import cleanup_real_profile_snapshots
            cleanup_real_profile_snapshots()
        except Exception as e:
            _bt.logger.debug("real-profile cleanup-on-consent-off failed: %s", e)
        _bt._real_profile_cdp_cache.pop("cdp", None)
        return None, None

    # Lightpanda rejects ``--profile``; check BEFORE default-browser detection so a
    # host with no Chromium default still reports the actionable engine conflict.
    if _lp._using_lightpanda_engine():
        return None, (_RP + "browser.engine is set to 'lightpanda', which cannot load a real Chromium profile. "
                      "Set browser.engine to 'auto' or 'chrome' to use real-profile browsing, or turn the toggle off.")

    from hermes_cli.browser_connect import (chromium_executable, detect_default_chromium,
                                            real_profile_copy_dir, snapshot_real_profile)

    if not _bt._real_profile_cdp_lock.acquire(
        timeout=_bt._REAL_PROFILE_CDP_LOCK_TIMEOUT_S
    ):
        return None, (
            _RP + "the real-profile browser is already being prepared by another "
            "call that has not finished. Retry after that call completes, or "
            "restart Hermes if it was abandoned."
        )
    try:
        cached = _bt._real_profile_cdp_cache.get("cdp")
        if cached and _cdp_http_ready(cached):
            # Re-claim the shared daemon's socket dir so the orphan reaper's idle clock sees
            # this process still using it (a cache hit never runs a daemon command).
            _session._prepare_session_socket_dir(_bt._REAL_PROFILE_SESSION)
            _mark_real_profile_used()  # local patch: idle-release bookkeeping
            return cached, None
        _bt._real_profile_cdp_cache.pop("cdp", None)

        browser = detect_default_chromium()
        unsupported = _real_profile_unsupported_reason(browser)
        if unsupported:
            return None, unsupported

        # Reuse BEFORE writing anything. CRITICAL: the snapshot overlay (truncates/rewrites
        # Cookies / Login Data) must NOT run while a live copy-browser (maybe from a previous
        # hermes process) holds the user-data-dir open — that corrupts the databases.
        copy_dir = real_profile_copy_dir(browser)
        existing = _agent_browser_get_cdp(_bt._REAL_PROFILE_SESSION)
        if existing and _cdp_http_ready(existing) and _cdp_on_data_dir(existing, copy_dir):
            _bt._real_profile_cdp_cache["cdp"] = existing
            return existing, None
        if existing:  # stale/wrong-dir session: close it so nothing holds the dir open
            _agent_browser_close_session(_bt._REAL_PROFILE_SESSION)
        # A Chrome from an earlier hermes process can still hold the copy dir after its attach
        # daemon was reaped (that owner died). Re-attach to it rather than overlay a live profile;
        # if the daemon cannot attach, fail closed — never snapshot over an open profile. Not ours
        # to terminate (no Popen handle): it lives until the user closes it, by design.
        surviving = _surviving_chrome_cdp(copy_dir)
        if surviving:
            cdp, err = _attach_agent_browser_to_real_profile(int(surviving.rsplit(":", 1)[1]), copy_dir)
            if not cdp:
                return None, err
            _bt._real_profile_cdp_cache["cdp"] = cdp
            _bt.logger.info("real-profile: re-attached to surviving Chrome at %s (%s)", cdp, copy_dir)
            _mark_real_profile_used()  # local patch: idle-release bookkeeping
            return cdp, None

        copy_dir, err = snapshot_real_profile(browser)
        if err or not copy_dir:
            # LOCAL PATCH (Sky, 2026-09-27, not upstream): a running browser holds the
            # login databases, which blocks the auth re-sync and used to stop the whole
            # launch. When the copy on disk is still fresh, use it instead of asking the
            # user to quit their browser - the log says which files missed the refresh.
            fallback_dir = _fresh_copy_dir(browser)
            if fallback_dir:
                _bt.logger.warning(
                    "real-profile: auth re-sync skipped (%s); using the existing copy at %s "
                    "(cookies younger than %ss)", err, fallback_dir, REAL_PROFILE_STALE_FALLBACK_SECONDS)
                copy_dir = fallback_dir
                err = None
            else:
                return None, _real_profile_snapshot_error(err)
        real_binary = chromium_executable(browser)
        if real_binary is None:
            return None, f"{_RP}the real browser binary for '{browser}' could not be found. Reinstall it or turn the toggle off."
        port, err = _launch_real_profile_chrome(real_binary, copy_dir)
        if port is None:
            return None, err
        cdp, err = _attach_agent_browser_to_real_profile(port, copy_dir)
        if not cdp:
            return None, err
        _bt._real_profile_cdp_cache["cdp"] = cdp
        _bt.logger.info("real-profile browser ready for %s at %s (%s)", browser, cdp, copy_dir)
        _mark_real_profile_used()  # local patch: idle-release bookkeeping
        return cdp, None
    finally:
        _bt._real_profile_cdp_lock.release()
