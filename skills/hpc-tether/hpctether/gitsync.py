"""Mirrored repositories (site ``[repos]``): origin URLs and their comparison.

A ``[repos]`` entry pairs a local repository with a cluster checkout. If the
pairing is wrong -- a mistyped path, a checkout of a different project -- the
guard would apply the entry's rules to the wrong repository. So before any git
write in a listed checkout runs, the cluster checkout's ``origin`` must name
the same repository as the local one.

URLs are compared after normalizing: ``git@github.com:o/r.git``,
``https://github.com/o/r`` and ``ssh://git@github.com/o/r.git`` are the same
repository. The Python and shell normalizers must agree;
``tests/test_gitsync.py`` runs both over the same cases.
"""

from __future__ import annotations

import subprocess


def normalize_url(url: str) -> str:
    """``host/owner/repo``, lower case, from any common spelling of a remote."""
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    u = u.rstrip("/")
    if "://" in u:
        u = u.split("://", 1)[1]
        hostpart = u.split("/", 1)[0]
        if "@" in hostpart:
            u = u.split("@", 1)[1]
        host, _, path = u.partition("/")
        u = host.split(":", 1)[0] + "/" + path
    elif "@" in u and ":" in u.split("@", 1)[1]:
        u = u.split("@", 1)[1]
        host, _, path = u.partition(":")
        u = host + "/" + path
    return u.lower()


#: The same normalization in bash, for the check that runs on the cluster.
SHELL_NORMALIZE = r'''hpc_norm() {
  local u="${1%/}"; u="${u%.git}"; u="${u%/}"
  case "$u" in
    *://*) u="${u#*://}"
           local hp="${u%%/*}"
           case "$hp" in *@*) u="${u#*@}";; esac
           local h="${u%%/*}"; h="${h%%:*}"
           case "$u" in */*) u="$h/${u#*/}";; *) u="$h/";; esac;;
    *@*:*) u="${u#*@}"; u="${u%%:*}/${u#*:}";;
  esac
  printf '%s' "$u" | tr '[:upper:]' '[:lower:]'
}'''


class OriginError(Exception):
    """The local side of a ``[repos]`` entry has no usable origin."""


def local_origin(local: str) -> str:
    """The local repository's ``origin`` URL, or :class:`OriginError`."""
    try:
        proc = subprocess.run(
            ["git", "-C", local, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OriginError(f"cannot run git in {local}: {exc}") from None
    url = proc.stdout.strip()
    if proc.returncode != 0 or not url:
        raise OriginError(f"{local} is not a git repository with an origin remote")
    return url
