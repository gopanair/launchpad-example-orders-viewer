"""Finding the volume, and the database on it.

Launchpad declares what is attached in LAUNCHPAD_STORAGE: a JSON array of
`{name, kind, path, access}`, written by the same code that built the pod's
mounts, so the description and the filesystem cannot disagree. That is the only
door this app uses. It never scans /mnt and never guesses a path — a folder it
was not given is not its business.

Two kinds can appear. A **volume** is an EFS access point: real POSIX, real
`flock`, which is what makes an embedded database on it correct. A **store** is
an S3 prefix through Mountpoint, which has no locking at all — SQLite on one is
corruption waiting for a second reader, so a store is never a candidate here.

This app takes the **first declared volume** and looks for one file on it. That
is a deliberately small rule: an app that searched every mount would find the
wrong database the day someone attaches a second one.
"""

import json
import os

ENV_STORAGE = "LAUNCHPAD_STORAGE"

KIND_VOLUME = "volume"
ACCESS_READ = "read"


class Mount:
    """One entry of LAUNCHPAD_STORAGE, plus what the filesystem says now."""

    def __init__(self, name, kind, path, access):
        self.name = name or "(unnamed)"
        self.kind = kind or ""
        self.path = path or ""
        self.access = access or ACCESS_READ

    @property
    def is_volume(self):
        return self.kind == KIND_VOLUME

    @property
    def present(self):
        return bool(self.path) and os.path.isdir(self.path)


def declared_mounts(raw=None):
    """Parse LAUNCHPAD_STORAGE. Returns (mounts, error).

    An unparseable value is an error rather than an empty list: an app that
    cannot read its own contract should say so, not present itself as an
    install with nothing attached.
    """
    if raw is None:
        raw = os.getenv(ENV_STORAGE, "")
    raw = raw.strip()
    if not raw:
        return [], None
    try:
        entries = json.loads(raw)
    except ValueError as err:
        return [], "%s is not the JSON array the platform writes: %s" % (ENV_STORAGE, err)
    if not isinstance(entries, list):
        return [], "%s is not a JSON array." % ENV_STORAGE
    mounts = []
    for entry in entries:
        if isinstance(entry, dict):
            mounts.append(
                Mount(entry.get("name"), entry.get("kind"), entry.get("path"), entry.get("access"))
            )
    return mounts, None


def resolve_db(filename, raw=None):
    """Work out where the database should be, and whether it is there.

    Returns a dict describing exactly one situation. `ready` is True only when
    there is a file to open; every other outcome carries a `problem` sentence
    written for whoever has to fix it, which is the whole point of the page
    this feeds.
    """
    state = {
        "ready": False,
        "mount": None,
        "mounts": [],
        "db_path": None,
        "problem": "",
        "hint": "",
    }

    mounts, err = declared_mounts(raw)
    state["mounts"] = mounts
    if err:
        state["problem"] = err
        state["hint"] = "This is a platform-side fault, not a configuration you can fix on this app."
        return state

    if not mounts:
        state["problem"] = "No storage is attached to this app."
        state["hint"] = (
            "Attach an EFS volume in Launchpad under the app's Storage tab, then redeploy or "
            "restart — a storage mapping takes effect on the next deployment, never on a running pod."
        )
        return state

    volumes = [m for m in mounts if m.is_volume]
    if not volumes:
        state["problem"] = "Storage is attached, but none of it is an EFS volume."
        state["hint"] = (
            "The mounts here are object stores (S3 through Mountpoint), which provide no file "
            "locking — SQLite on one corrupts. Attach a volume instead."
        )
        return state

    # The first declared volume, in the platform's own order. Not the first
    # usable one: if the volume this app is meant to read is missing, that is
    # the fact to report, not a reason to quietly read a different one.
    mount = volumes[0]
    state["mount"] = mount

    if not mount.present:
        state["problem"] = "The volume “%s” is declared at %s, but nothing is there." % (
            mount.name,
            mount.path,
        )
        state["hint"] = (
            "The mapping was probably added after this app last started. Redeploy or restart it."
        )
        return state

    path, why = contained_path(mount.path, filename)
    if path is None:
        state["problem"] = why
        state["hint"] = "Set ORDERS_DB to a path inside the volume."
        return state
    state["db_path"] = path

    if not os.path.exists(path):
        state["problem"] = "There is no %s on the volume “%s” yet." % (filename, mount.name)
        state["hint"] = (
            "Put the database at that path — Launchpad's own volume browser can upload it — "
            "then check again. Nothing is cached, so a redeploy is not needed: the next request "
            "looks again."
        )
        return state
    if not os.path.isfile(path):
        state["problem"] = "%s exists but is not a file." % path
        return state

    state["ready"] = True
    return state


def contained_path(root, filename):
    """Join a configured filename onto the volume root, refusing to leave it.

    ORDERS_DB is the operator's, not a request parameter, so this is not a
    security boundary so much as a way for a typo to be a sentence instead of
    a traceback from somewhere else on the filesystem.
    """
    if not filename:
        return None, "ORDERS_DB is empty."
    if os.path.isabs(filename):
        return None, "ORDERS_DB must be relative to the volume, not an absolute path."
    joined = os.path.normpath(os.path.join(root, filename))
    if joined != root and not joined.startswith(root.rstrip(os.sep) + os.sep):
        return None, "ORDERS_DB points outside the volume."
    return joined, ""
