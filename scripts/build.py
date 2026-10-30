#!/usr/bin/env python3
"""Bootstrap stub - docs/imp_design.md §2.4 (supersedes the pre-WP-03
~516-line build orchestrator, now `adas_build_tools`, published from
`modules/toolchain` and installed per-component via pip+venv).

Physically identical across every component in this repo (df, vdy,
interfaces, mathlib, perception-core, shared_config, sim) - propagating a
future fix means editing modules/toolchain/copy_files/scripts/build.py,
running sync_copy_files.py, and committing the fan-out into every
component's own repo (R-BUILD-6), same discipline as build.bat/build.sh.

Runs on bare system Python, stdlib only (venv, subprocess, pathlib, sys,
os, time) - it cannot assume anything else is installed yet, that's the
whole reason this file stays this small. Its only job: find or create a
.venv, make sure the pinned version of adas_build_tools is installed into
it, then exec into it and forward argv/exit-code untouched - never any
build logic of its own.

Usage: python build.py -t <target> -p <platform> [-P <project>] [--clean]
       python build.py config
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Default `toolchain` git remote pip installs a version from, by tag
# (docs/imp_design.md §4, decided 2026-09-08 - no package index, no
# server). Overridable for anyone whose network reaches a mirror instead.
# Used only for a "vX.Y.Z" pin (see install_pinned()) - a bare "X.Y.Z" pin
# installs via Conan instead (§5 addendum, §9.8), needing no git remote at
# all.
TOOLCHAIN_GIT_URL = os.environ.get(
    "ADAS_TOOLCHAIN_GIT_URL", "https://github.com/Pavan-karimajji/toolchain")

# Conan remote a bare "X.Y.Z" pin installs adas-build-tools from (§5
# addendum, §9.8) - the same remote every other adas-* package already
# needs reachable, so a machine that can build this repo at all can also
# install this package this way. ADAS_LOCAL_URL has NO default - same "no
# plausible default" discipline adas_build_tools.conan_ops's own copy of
# this constant already documents (project_conan_server_infra: no stable,
# network-reachable address exists for every machine yet). Credentials DO
# have a real, working default (demo/demo, the actual shipped adas-local
# login).
ADAS_LOCAL_REMOTE = os.environ.get("ADAS_LOCAL_REMOTE", "adas-local")
ADAS_LOCAL_URL = os.environ.get("ADAS_LOCAL_URL")
ADAS_LOCAL_USER = os.environ.get("ADAS_LOCAL_USER", "demo")
ADAS_LOCAL_PASSWORD = os.environ.get("ADAS_LOCAL_PASSWORD", "demo")

# How long a second, concurrent invocation waits for the first to finish
# bootstrapping .venv before giving up (TCT-10).
LOCK_TIMEOUT_S = 600

# NS-06 (cache_first_no_server_plan.md D4/WP-4, STEP 4): the test-only
# override, same name and same precedence role as scripts/
# conan_home_resolution.REMOTES_ENV_VAR - kept as a literal string rather
# than importing that module, since this stub must run before anything
# outside the standard library is available and a standalone submodule
# clone has no scripts/conan_home_resolution.py to import at all (same
# reasoning as _team_server_url()'s own duplication note below).
REMOTES_ENV_VAR = "ADAS_REMOTES_ENABLED"


def find_component_root():
    """The directory containing this scripts/build.py file's parent (the
    component root) - this file is still copy-distributed and lives at a
    fixed location relative to the component, unlike the package it
    bootstraps (docs/imp_design.md §2.4 step 1)."""
    return Path(__file__).resolve().parent.parent


def read_pin(component_root):
    """conf/build_tools.version, one line, stripped. Missing file is a hard
    error naming the expected path - never a silently-assumed default
    version (docs/imp_design.md §2.4 step 2, TCT-06)."""
    pin_path = component_root / "conf" / "build_tools.version"
    if not pin_path.is_file():
        sys.exit(
            f"ERROR: no build-tools version pin found at {pin_path}.\n"
            f"Add one - e.g. a Conan-published version (§5 addendum):\n"
            f"          echo 1.0.3> \"{pin_path}\"\n"
            f"       or a git tag (§4):\n"
            f"          echo v1.0.0> \"{pin_path}\""
        )
    return pin_path.read_text(encoding="utf-8").strip()


def venv_paths(component_root):
    """`.venv`'s location and the platform-correct interpreter path
    (docs/imp_design.md §2.4 step 3, TCT-08).

    Suffixed by `sys.platform` (item 3, 2026-09-16) -- found live: a
    Windows build and a WSL build of the SAME checkout (the two access
    Path.exists/mkdir/rmtree the identical directory through DrvFs, which
    IS mutually visible with no caching lag, confirmed live with a
    concurrent mkdir/Test-Path probe) used to share one bare `.venv`.
    `_venv_targets_current_python()` correctly treats a foreign-OS venv as
    stale and recreates it -- but the `.venv.lock` directory below only
    guards `ensure_venv()`'s own create/install window, not the much
    longer window afterward where `exec_into_venv()` is actually RUNNING
    the build out of that venv's interpreter with no lock held at all. So
    the failure needs no timing coincidence beyond "the other platform is
    still running its own build": platform B's `ensure_venv()` correctly
    decides platform A's venv is foreign, deletes it, and platform A's
    still-running process loses the DLL/site-packages files it has open
    mid-install. Splitting the directory by platform removes the shared
    resource instead of trying to serialize access to it -- the two
    platforms never touch the same `.venv-*` directory, so there is
    nothing left to race. `sys.platform` (not sys.platform.startswith so
    the exact tag, "win32"/"linux", is stable and greppable) is already
    used two lines below for the interpreter subpath decision, so this
    reuses a signal already computed, not a new one."""
    venv_dir = component_root / f".venv-{sys.platform}"
    if sys.platform.startswith("win"):
        interpreter = venv_dir / "Scripts" / "python.exe"
    else:
        interpreter = venv_dir / "bin" / "python"
    return venv_dir, interpreter


def venv_is_current(venv_dir, pin):
    """True if the last SUCCESSFUL install into `venv_dir` already matches
    `pin` (docs/imp_design.md §2.4 step 4) - read from a marker file written
    only on success by install_pinned(), never assumed."""
    marker = venv_dir / "adas_build_tools.version"
    return marker.is_file() and marker.read_text(encoding="utf-8").strip() == pin


def _venv_targets_current_python(venv_dir, interpreter):
    """False for a missing/corrupted interpreter, or one built from a
    DIFFERENT system Python than the one running this stub right now - both
    cases must recreate .venv rather than trust it as-is (TCT-07)."""
    if not interpreter.is_file():
        return False
    cfg = venv_dir / "pyvenv.cfg"
    if not cfg.is_file():
        return False
    home = None
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("home"):
            home = line.split("=", 1)[1].strip()
            break
    if not home:
        return False
    return Path(home).resolve() == Path(sys.executable).resolve().parent


def _acquire_lock(component_root):
    """A directory-create-based lock (atomic on every platform this repo
    targets) so two simultaneous invocations against the same empty .venv
    don't race each other into a corrupted install (TCT-10) - the second
    process waits, then finds .venv already current and does nothing.

    Suffixed by `sys.platform` (item 3, 2026-09-16) to match venv_paths()'s
    own per-platform directory -- two builds for the SAME platform still
    need to serialize their .venv-<platform> install; two builds for
    DIFFERENT platforms no longer share a directory at all, so they no
    longer need to wait on each other's lock either."""
    lock_dir = component_root / f".venv-{sys.platform}.lock"
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            lock_dir.mkdir()
            return lock_dir
        except FileExistsError:
            if time.monotonic() > deadline:
                sys.exit(
                    f"ERROR: timed out after {LOCK_TIMEOUT_S}s waiting for the .venv "
                    f"bootstrap lock at {lock_dir} (held by another build). Remove it "
                    f"by hand only if you're sure no other build is actually running."
                )
            time.sleep(0.5)


def install_pinned(venv_dir, pin):
    """Installs `pin` into `venv_dir` via one of two mechanisms, chosen by
    the pin's own shape (docs/imp_design.md §5 addendum, §9.8) - a "vX.Y.Z"
    git tag installs via `pip install git+<toolchain-url>@<pin>` (§4,
    original mechanism, needs the toolchain git remote reachable); a bare
    "X.Y.Z" installs via Conan (`conan download` -> `conan cache path` ->
    `pip install <wheel>`), reusing whatever adas-local infrastructure is
    already required for every other adas-* package - for a machine that
    can't reach the toolchain git remote (docs/imp_design.md §5's original
    motivation) but already has adas-local configured for its C++ deps.
    The marker is written only after a successful install, so a failed
    install is retried next run rather than silently treated as current
    (TCT-09)."""
    _, interpreter = venv_paths(venv_dir.parent)
    if pin.startswith("v"):
        # LO-02 (imp_design.md section 12): on a machine with no cached git
        # credentials for a private repo, `git clone` over https blocks
        # indefinitely waiting for a username/password prompt that has
        # nothing to answer it - confirmed live, no error at all, for as
        # long as this session measured (50 minutes) before anyone
        # noticed. GIT_TERMINAL_PROMPT=0 makes git fail fast instead
        # (confirmed live: same command, same broken URL - hangs past a
        # timeout with the var unset, fails in under a second with a
        # clear "could not read Username ...: terminal prompts disabled"
        # with it set to "0"). Env is inherited (`{**os.environ, ...}`),
        # not replaced, so nothing else about this subprocess's
        # environment changes.
        proc = subprocess.run(
            [str(interpreter), "-m", "pip", "install", "--quiet", "--no-compile",
             f"git+{TOOLCHAIN_GIT_URL}@{pin}"],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if proc.returncode != 0:
            sys.exit(f"ERROR: pip install of adas-build-tools@{pin} failed (exit {proc.returncode}).")
    else:
        _install_pinned_via_conan(interpreter, pin, venv_dir.parent)
    (venv_dir / "adas_build_tools.version").write_text(pin, encoding="utf-8")


def _team_server_url(component_root):
    """Fallback source for the adas-local URL when ADAS_LOCAL_URL isn't
    set (docs/imp_design.md §11 addendum): scripts/team_conan_server.txt
    at the superproject root, a checked-in file scripts/setup.py's own
    --role dev path already reads for this exact purpose. Walks upward
    from `component_root` looking for it, same generosity as
    find_component_root()'s own walk - a standalone component clone (no
    superproject present) simply has no such file to find, so this
    returns None rather than raising in that case. Duplicated from
    adas_build_tools.conan_ops's own copy for the same reason every
    function in this stub is duplicated (§9.2's own note: must run before
    that package is installed)."""
    candidate = component_root
    for _ in range(6):
        team_file = candidate / "scripts" / "team_conan_server.txt"
        if team_file.is_file():
            for line in team_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    return stripped
            return None
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return None


def _is_loopback_url(url):
    """NS-24/D1a duplicated (cache_first_no_server_plan.md, same reasoning
    as scripts/conan_home_resolution._is_loopback_url): a per-machine
    loopback Conan server in scripts/team_conan_server.txt is not a team
    server - without this, remotes_enabled() below would read ON on every
    machine that ever ran a local adas-local instance, defeating NS-06's
    whole point."""
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname
    return host is not None and host.lower() in ("127.0.0.1", "localhost", "::1")


def remotes_enabled(component_root, env=None):
    """NS-06 (cache_first_no_server_plan.md D4/WP-4, STEP 4): duplicated
    from scripts/conan_home_resolution.remotes_enabled() for the same
    reason _team_server_url() above is duplicated - this stub must decide
    whether it may contact a remote BEFORE adas_build_tools (and the
    superproject's own scripts/, which may not even be checked out for a
    standalone submodule clone, NS-07) is available to ask. No CLI-override
    level here (unlike the root copy): this stub forwards argv to
    adas_build_tools.cli untouched and never parses it itself (see this
    file's own module docstring), so the only overrides are the env var
    and scripts/team_conan_server.txt. Returns (bool enabled, str reason).
    """
    env = os.environ if env is None else env
    raw = env.get(REMOTES_ENV_VAR)
    if raw is not None:
        value = raw.strip().lower()
        if value in ("on", "1", "true", "yes"):
            return True, f"{REMOTES_ENV_VAR}={raw!r} (env override)"
        if value in ("off", "0", "false", "no"):
            return False, f"{REMOTES_ENV_VAR}={raw!r} (env override)"
        # Not a recognized spelling - fall through, same as the root copy.

    url = _team_server_url(component_root)
    if url:
        if _is_loopback_url(url):
            return False, (f"{url} is a loopback address, not a team "
                            f"server (NS-24/D1a)")
        return True, f"team server configured: {url}"
    return False, "no team Conan server configured (scripts/team_conan_server.txt is a placeholder or absent)"


def _ensure_adas_local_remote(component_root):
    """Minimal, strictly-additive auto-provisioning of the adas-local
    remote itself - a slice of adas_build_tools.conan_ops's own
    ensure_conan_environment() (§2.5), duplicated here because this runs
    BEFORE adas_build_tools is installed (the whole reason this stub
    exists): a bare "X.Y.Z" pin's `conan download` needs adas-local
    reachable before that later, fuller auto-provisioning step ever gets a
    chance to run - without this, a genuinely bare machine using the Conan
    pin would need adas-local pre-registered by hand, silently reopening
    the exact manual-setup gap §2.5 exists to close. Never overwrites an
    existing remote or re-registers one already present; no URL from
    either ADAS_LOCAL_URL or scripts/team_conan_server.txt just means this
    is a no-op, same "no plausible default" discipline conan_ops.py's own
    copy already documents.

    NS-43 (cache_first_no_server_plan.md, RQ-5): defence in depth. Today
    there is exactly one call site and it already sits inside the
    remotes-enabled branch (WHERE/WHETHER split, RQ-5) - this guard makes
    that true by construction instead of by "the one caller happens to
    check first", so a future call site, or the outer `if` at that one
    call site being refactored away, cannot silently re-open remote
    contact on a remotes-off machine. Returns immediately, touching
    nothing, when remotes are off."""
    enabled, _ = remotes_enabled(component_root)
    if not enabled:
        return
    list_proc = subprocess.run(
        ["conan", "remote", "list", "--format=json"],
        capture_output=True, text=True,
    )
    try:
        remotes = [entry.get("name") for entry in json.loads(list_proc.stdout)] \
            if list_proc.returncode == 0 else []
    except (ValueError, AttributeError):
        remotes = []

    if ADAS_LOCAL_REMOTE not in remotes:
        url = ADAS_LOCAL_URL or _team_server_url(component_root)
        if not url:
            return
        subprocess.run(
            ["conan", "remote", "add", ADAS_LOCAL_REMOTE, url],
            check=False,
        )
        remotes.append(ADAS_LOCAL_REMOTE)

    if ADAS_LOCAL_REMOTE not in remotes:
        return
    users_proc = subprocess.run(
        ["conan", "remote", "list-users", ADAS_LOCAL_REMOTE, "--format=json"],
        capture_output=True, text=True,
    )
    try:
        authenticated = bool(
            json.loads(users_proc.stdout).get(ADAS_LOCAL_REMOTE, {}).get("authenticated")
        ) if users_proc.returncode == 0 else False
    except (ValueError, AttributeError):
        authenticated = False
    if not authenticated:
        subprocess.run(
            ["conan", "remote", "login", ADAS_LOCAL_REMOTE,
             ADAS_LOCAL_USER, "-p", ADAS_LOCAL_PASSWORD],
            check=False, capture_output=True,
        )


# NS-12/D4c (cache_first_no_server_plan.md, piece 2): how many directory
# levels _find_offline_bundle() walks upward looking for a superproject's
# own misc/offline_bundle/ - same generosity as _team_server_url()'s own
# walk above.
_BUNDLE_SEARCH_DEPTH = 6


# D14 (python_tool_envs_plan.md, PT-17): host -> bundle platform. The ONE
# duplicate of adas_build_tools.conan_ops.HOST_BUNDLE_PLATFORM (this stub
# runs before that package is installed); check_invariants.py HPM-1 holds
# the two equal.
HOST_BUNDLE_PLATFORM = {"win32": "vs2026", "linux": "ubuntu_gcc"}


def _find_offline_bundle(component_root):
    """NS-12/D4c piece 2: a folder written by scripts/make_offline_
    bundle.py, checked in the same two places adas_build_tools.conan_ops'
    own find_offline_bundle() checks - this component's own misc/
    offline_bundle/ first (a standalone clone ships its own copy
    alongside it, since there is no superproject root to hold one), then
    walked upward looking for a real superproject checkout's misc/
    offline_bundle/ at whatever depth this component happens to be
    nested (modules/df, modules/sim/vantage, ...) - never assumed to be
    exactly one level up. Returns None if neither is found; never
    raises. Duplicated rather than imported for the usual reason (this
    stub must run BEFORE adas_build_tools is installed). D14: returns the
    HOST platform's subfolder; a flat pre-D14 bundle is refused (exit 1,
    Reason / Fix / Run), never used."""
    top = None
    own = component_root / "misc" / "offline_bundle"
    if own.is_dir():
        top = own
    candidate = component_root
    for _ in range(_BUNDLE_SEARCH_DEPTH):
        if top is not None or candidate.parent == candidate:
            break
        candidate = candidate.parent
        found = candidate / "misc" / "offline_bundle"
        if found.is_dir():
            top = found
    if top is None:
        return None
    platform = HOST_BUNDLE_PLATFORM.get(sys.platform, "ubuntu_gcc")
    if (top / "bundle_manifest.txt").is_file() or any(top.glob("adas_offline_*.tgz")):
        if sys.platform == "win32":
            run = (f'cd /d "{top}" && mkdir {platform} && move adas_offline_*.tgz {platform}\\ '
                   f'&& move bundle_manifest.txt {platform}\\ && move wheels {platform}\\')
        else:
            run = (f'cd "{top}" && mkdir -p {platform} && '
                   f'mv adas_offline_*.tgz bundle_manifest.txt wheels {platform}/')
        print(f"Reason: {top} holds an offline bundle in the old flat layout; bundles "
              f"now live in one subfolder per platform ({top / platform}).")
        print("Fix:    move it into its platform subfolder, then re-run.")
        print(f"Run:    {run}")
        sys.exit(1)
    sub = top / platform
    return sub if sub.is_dir() else None


def _parse_bundle_manifest(text):
    """bundle_manifest.txt -> {bare ref: revision}. Duplicated from
    scripts/make_offline_bundle.parse_manifest() (superproject-only, not
    importable from a standalone component clone) - deliberately a much
    smaller subset: this stub only ever needs "is this ref in the bundle
    at all", never date/source_commit/pins/wheels."""
    refs = {}
    for line in text.splitlines():
        key, _, value = line.partition(": ")
        if key == "ref":
            ref, _, rest = value.partition("#")
            refs[ref] = rest.split()[0] if rest else ""
    return refs


def _restore_ref_from_bundle(bundle_dir, ref):
    """NS-12/D4c piece 2: restores `bundle_dir`'s single adas_offline_*.tgz
    archive into the local Conan cache if its manifest carries `ref` - the
    chicken-and-egg this stub exists to solve (this pin is what makes
    adas_build_tools itself resolvable at all, so restoring it from a
    bundle can only happen here, in plain Conan CLI, before any venv or
    installed package exists to ask instead). A no-op (returns False,
    never raises) for a manifest missing bundle_manifest.txt, more or
    less than one archive, or a manifest that simply doesn't list `ref`
    (an older bundle than this pin). The caller re-checks
    _cached_package_ids() itself afterward rather than trusting this
    function's return value as proof of success, same "verify, don't
    assume" discipline as install_pinned()'s own marker file."""
    manifest_file = bundle_dir / "bundle_manifest.txt"
    if not manifest_file.is_file():
        return False
    manifest_refs = _parse_bundle_manifest(manifest_file.read_text(encoding="utf-8"))
    if ref not in manifest_refs:
        return False
    archives = sorted(bundle_dir.glob("adas_offline_*.tgz"))
    if len(archives) != 1:
        return False
    proc = subprocess.run(["conan", "cache", "restore", str(archives[0])])
    return proc.returncode == 0


def _cached_package_ids(ref):
    """OF-02 (offline_setup_plan.md): package ids of `ref` already in the LOCAL
    Conan cache - `conan list` with no `-r` never touches a remote. [] when
    the ref is absent or the listing cannot be read."""
    proc = subprocess.run(["conan", "list", f"{ref}:*", "--format=json"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return []
    return [package_id
            for cache_data in data.values()
            for revision_data in cache_data.get(ref, {}).get("revisions", {}).values()
            for package_id in revision_data.get("packages", {})]


def _setup_run_line(component_root):
    """D11/PT-21: the exact command that runs this component's setup, for
    this host's shell -- setup.bat on Windows, never on Linux."""
    if sys.platform == "win32":
        return f'cd /d "{component_root}" && setup.bat'
    return f'cd "{component_root}" && python3 scripts/setup.py'


def _install_pinned_via_conan(interpreter, pin, component_root):
    """Conan-based half of install_pinned() (docs/imp_design.md §5
    addendum, §9.8). `adas-build-tools/<pin>`'s package_id() calls
    self.info.clear(), so it always collapses to one fixed hash regardless
    of settings - never hardcoded here, queried fresh via `conan list`
    each time, since relying on a literal would silently break the moment
    Conan's own hashing of an empty ConanInfo ever changed."""
    if shutil.which("conan") is None:
        sys.exit(
            "ERROR: conf/build_tools.version names a bare version "
            f"({pin!r}), which installs via Conan, but 'conan' isn't on "
            "PATH. Install Conan first, or pin a \"vX.Y.Z\" git tag "
            "instead (§4)."
        )

    ref = f"adas-build-tools/{pin}"
    if _cached_package_ids(ref):
        # OF-02: already in the local cache (an offline bundle, or an earlier
        # download) - never contact, register or log in to adas-local.
        print(f"{ref} is already in the local Conan cache - not contacting "
              f"'{ADAS_LOCAL_REMOTE}'.", flush=True)
    else:
        # NS-06 (cache_first_no_server_plan.md D4/WP-4, STEP 4): cache-first,
        # remotes-off-by-default applies here too - a missing pin with
        # remotes off is a stop naming the pin, the cache checked, and
        # setup.bat, never an implicit `conan download`. With remotes on,
        # the original download path is unchanged.
        enabled, reason = remotes_enabled(component_root)
        if not enabled:
            # NS-12/D4c piece 2: try the offline bundle before refusing -
            # the chicken-and-egg this stub exists to solve (see
            # _restore_ref_from_bundle()'s own docstring).
            bundle_dir = _find_offline_bundle(component_root)
            if bundle_dir is not None:
                print(f"{ref} is not in this machine's Conan cache - trying "
                      f"the offline bundle at {bundle_dir} ...", flush=True)
                _restore_ref_from_bundle(bundle_dir, ref)
            if not _cached_package_ids(ref):
                bundle_note = (
                    f" A bundle was found at {bundle_dir} but does not "
                    "carry this ref." if bundle_dir is not None
                    else " No offline bundle was found either.")
                print(f"Reason: {ref} is not in this machine's Conan cache "
                      f"(checked the local Conan cache only - remotes are off: "
                      f"{reason}).{bundle_note}")
                print("Fix:    run this component's setup to populate the cache "
                      "(or enable remotes: scripts/team_conan_server.txt, or "
                      f"{REMOTES_ENV_VAR}=on, to allow a download).")
                print(f"Run:    {_setup_run_line(component_root)}")
                sys.exit(1)
            print(f"{ref} restored from the offline bundle - not contacting "
                  f"'{ADAS_LOCAL_REMOTE}'.", flush=True)
        else:
            _ensure_adas_local_remote(component_root)
            proc = subprocess.run(["conan", "download", ref, "-r", ADAS_LOCAL_REMOTE])
            if proc.returncode != 0:
                sys.exit(
                    f"ERROR: conan download of {ref} from '{ADAS_LOCAL_REMOTE}' "
                    f"failed (exit {proc.returncode})."
                )

    list_proc = subprocess.run(
        ["conan", "list", f"{ref}:*", "--format=json"],
        capture_output=True, text=True,
    )
    if list_proc.returncode != 0:
        sys.exit(
            f"ERROR: conan list of {ref} failed (exit {list_proc.returncode}):\n"
            f"{list_proc.stderr}"
        )
    data = json.loads(list_proc.stdout)
    package_ids = [
        package_id
        for cache_data in data.values()
        for revision_data in cache_data.get(ref, {}).get("revisions", {}).values()
        for package_id in revision_data.get("packages", {})
    ]
    if not package_ids:
        sys.exit(f"ERROR: no built packages found for {ref} on '{ADAS_LOCAL_REMOTE}'.")

    path_proc = subprocess.run(
        ["conan", "cache", "path", f"{ref}:{package_ids[0]}"],
        capture_output=True, text=True,
    )
    if path_proc.returncode != 0:
        sys.exit(
            f"ERROR: conan cache path of {ref}:{package_ids[0]} failed "
            f"(exit {path_proc.returncode}):\n{path_proc.stderr}"
        )
    package_dir = Path(path_proc.stdout.strip())
    wheels = list(package_dir.glob("adas_build_tools-*.whl"))
    if not wheels:
        sys.exit(f"ERROR: no adas_build_tools wheel found in {package_dir}.")

    proc = subprocess.run(
        [str(interpreter), "-m", "pip", "install", "--quiet", "--no-compile", str(wheels[0])],
    )
    if proc.returncode != 0:
        sys.exit(f"ERROR: pip install of {wheels[0]} failed (exit {proc.returncode}).")


def _rmtree_tolerant(path):
    """Like `shutil.rmtree(path)`, but a directory-shaped entry
    `os.scandir()` cannot even open - a foreign-OS symlink such as a WSL
    venv's `lib64` -> `lib` (Windows cannot traverse it through the
    WSL/NTFS 9P bridge, `WinError 1920` - the exact incident that hits
    this function: recreating a `.venv` a WSL build left behind, from
    native Windows, per `_venv_targets_current_python()`'s own mismatch
    check just above) - is removed with a plain, non-recursive
    `os.rmdir()` instead. Proven live: `os.rmdir()`/`Directory.Delete()`
    succeeds on exactly this path even though `os.scandir()` into it does
    not, since removing a directory entry never needs to enumerate what's
    inside it. Never silently gives up - a removal that still fails after
    that fallback re-raises the ORIGINAL error, same as an unhandled
    `shutil.rmtree()` would."""
    def _onerror(_func, failed_path, exc_info):
        # shutil.rmtree() always retries os.rmdir(path) unconditionally
        # after ANY failure for that path, even one this handler already
        # fixed (its scandir() failure branch has no entries to reason
        # about, so it falls through to the same unconditional rmdir()
        # every other directory gets) - found live via this function's own
        # test, TypeError-free but a real second call: onerror() fires
        # again for the SAME path with FileNotFoundError once the first
        # call's os.rmdir() already removed it. Already-gone is success,
        # not a new failure to report.
        if not os.path.exists(failed_path):
            return
        try:
            os.rmdir(failed_path)
        except OSError:
            raise exc_info[1]
    shutil.rmtree(path, onerror=_onerror)


def _bundle_pip_wheel(component_root):
    """A pip wheel from the offline bundle's own flat wheels/ folder
    (make_offline_bundle.py's WHEEL_REQUIREMENTS carries one, pure-Python
    so it fits any interpreter), tried BEFORE ensurepip: found live,
    2026-10-04 - Debian/Ubuntu's packaged system Python deliberately
    ships ensurepip with no bundled wheels at all (Debian policy: pip
    comes from apt, not from CPython's own bundle), so relying on
    ensurepip alone refuses to create a venv on exactly the host most in
    need of the offline bundle. None if no bundle is found or it carries
    no pip wheel."""
    bundle_dir = _find_offline_bundle(component_root)
    if bundle_dir is None:
        return None
    wheels = sorted((bundle_dir / "wheels").glob("pip-*.whl"))
    return wheels[-1] if wheels else None


def _bundled_pip_wheel(python_exe):
    """The pip wheel bundled inside `python_exe`'s own ensurepip package -
    queried on that interpreter itself, not assumed to match the one
    running this stub. None if ensurepip is missing/stripped (Debian/
    Ubuntu's own packaged Python, see _bundle_pip_wheel()'s docstring) or
    carries no wheel."""
    proc = subprocess.run(
        [str(python_exe), "-c",
         "import ensurepip, os; print(os.path.join(os.path.dirname(ensurepip.__file__), '_bundled'))"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    bundled = Path(proc.stdout.strip())
    if not bundled.is_dir():
        return None
    wheels = sorted(bundled.glob("pip-*.whl"))
    return wheels[-1] if wheels else None


def _create_venv(venv_dir, python_exe, component_root=None):
    """`venv_dir` created WITHOUT pip, then pip bootstrapped into it
    --no-compile from a wheel - the same sys.path-prepend-and-runpy trick
    ensurepip uses internally, just with --no-compile added (setup_
    followup_prompt.md step 4: `adas_build_tools.venv_util.create_venv()`'s
    twin, duplicated here because this stub is stdlib-only and cannot
    import that package - same reason every other function in this file
    is duplicated, R-BUILD-6, propagated via sync_copy_files.py).
    `python -m venv` WITH pip (the default) fails deterministically on
    WSL's DrvFS-mounted /mnt/c: pip's own install-time compile-and-read-
    back step races the 9p mount (`assert os.path.exists(pyc_path)`,
    reproduced live 2026-10-04). --no-compile is used on every platform,
    not only DrvFS paths - one code path is sleeker, and a tool venv's
    first import simply recompiles once, same cost as any freshly
    installed package. The offline bundle's own pip wheel is tried
    before ensurepip's (see _bundle_pip_wheel()'s docstring - Debian/
    Ubuntu's system Python ships no bundled wheel at all)."""
    subprocess.run([str(python_exe), "-m", "venv", "--without-pip", str(venv_dir)], check=True)
    pip_wheel = (_bundle_pip_wheel(component_root) if component_root else None) \
        or _bundled_pip_wheel(python_exe)
    if pip_wheel is None:
        sys.exit(f"ERROR: no pip wheel found (neither the offline bundle nor "
                 f"{python_exe}'s own ensurepip bundle).")
    _, interpreter = venv_paths(venv_dir.parent)
    script = (
        "import runpy, sys\n"
        f"sys.path[0:0] = [{str(pip_wheel)!r}]\n"
        "sys.argv[1:] = ['install', '--no-cache-dir', '--no-compile', '--no-index', "
        f"'--find-links', {str(pip_wheel.parent)!r}, 'pip']\n"
        "runpy.run_module('pip', run_name='__main__')\n"
    )
    proc = subprocess.run([str(interpreter), "-c", script])
    if proc.returncode != 0:
        sys.exit(f"ERROR: bootstrapping pip into {venv_dir} failed (exit {proc.returncode}).")


def ensure_venv(component_root, pin):
    """Creates `.venv` if absent or if it targets a different Python
    (TCT-07), then installs `pin` into it if not already current
    (docs/imp_design.md §2.4 step 5)."""
    lock_dir = _acquire_lock(component_root)
    try:
        venv_dir, interpreter = venv_paths(component_root)
        if not _venv_targets_current_python(venv_dir, interpreter):
            if venv_dir.exists():
                _rmtree_tolerant(venv_dir)
            _create_venv(venv_dir, sys.executable, component_root)
        if not venv_is_current(venv_dir, pin):
            install_pinned(venv_dir, pin)
        return venv_dir
    finally:
        lock_dir.rmdir()


def exec_into_venv(venv_dir, argv, module="adas_build_tools.cli"):
    """Runs `module` from inside `venv_dir` with `argv` forwarded untouched,
    propagating its exit code unmodified (docs/imp_design.md §2.4 step 7) -
    os.execv on POSIX; Windows has no true exec-replace, so subprocess.run +
    sys.exit there. `module` defaults to the CLI entry point; build_gui.py
    (docs/imp_design.md §13, BS-01/BS-02) delegates into this same function
    with `module="adas_build_tools.gui"` instead of duplicating it."""
    _, interpreter = venv_paths(venv_dir.parent)
    if os.name == "posix":
        os.execv(str(interpreter), [str(interpreter), "-m", module, *argv])
    else:
        proc = subprocess.run([str(interpreter), "-m", module, *argv])
        sys.exit(proc.returncode)


def main(argv=None, module="adas_build_tools.cli"):
    if argv is None:
        argv = sys.argv[1:]
    component_root = find_component_root()
    pin = read_pin(component_root)
    venv_dir = ensure_venv(component_root, pin)
    exec_into_venv(venv_dir, argv, module=module)


if __name__ == "__main__":
    main()
