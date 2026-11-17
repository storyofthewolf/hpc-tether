"""The command tables the payload guard classifies by: data, not logic.

``guard.py`` parses a payload into commands and applies the few rules that
depend on arguments (``git``, ``mv``, ``find``, ``sed`` and the like). Which
command *names* fall in which class lives here, so that teaching the guard a
new command is a one-line data change rather than another branch in
``guard.py``.

The guard is fail-closed below ``improvise`` (hpctether/profile.py): a
command that no rule claims and that is not a known read is refused as
unknown, because the guard cannot tell whether it writes or runs code. The
same holds one level down: a known read carrying an option its entry does
not list is unknown too (:data:`READ_OPTIONS`). A command goes in
:data:`READ_ANY` only if it can never write a file, change scheduler state,
or run code, under any flag; a command that reads files goes in
``READ_OPTIONS`` with the options it may carry. One that reads in one form
and writes in another also gets an entry in :data:`ARG_RULES`.

The comments on each set record the incident that put an entry there; keep
them with the entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Commands that are never sent, regardless of arguments.
FORBIDDEN_ALWAYS = frozenset(
    {
        # deletion, in every spelling: no flag, no path, no site setting
        "rm", "rmdir", "unlink", "shred",
        # irreversible byte-level damage
        "dd", "truncate", "chown", "chgrp",
        # persistence on the login node, outside the watchdog and this scan
        "crontab", "at", "batch",
    }
)

#: The deletion subset of the above. :func:`contains_deletion` checks for
#: these at the transport layer, so that no verb in the toolkit -- not only
#: hpc-run -- can transmit a delete, whatever script it assembles.
DELETION_CMDS = frozenset({"rm", "rmdir", "unlink", "shred"})

#: Commands that change remote state and therefore need --allow-write.
WRITE_CMDS = frozenset(
    {
        "sbatch", "scancel", "cp", "mkdir", "touch", "ln", "tee",
        "install", "tar", "kill", "pkill", "unzip", "gunzip", "gzip",
        "chmod",
        # archive and compression tools that write their output beside the input
        "bzip2", "bunzip2", "xz", "unxz", "zip", "split", "csplit", "mkfifo",
        # NCO and CDO: each writes an output file (ncrename and ncatted edit
        # the input in place). ncks is a read with one file; see ARG_RULES.
        "ncrcat", "ncra", "ncea", "nces", "ncecat", "ncwa", "ncbo", "ncdiff",
        "ncflint", "ncpdq", "ncap2", "ncrename", "ncatted", "nccopy", "cdo",
    }
)

#: Wrapper commands: the real command is further along the token list.
WRAPPERS = frozenset(
    {
        "sudo", "env", "nohup", "nice", "time", "timeout", "stdbuf", "command",
        "exec",
        # Each of these took a bare `rm` to read tier:
        # the verb was behind a prefix the scan stopped on. `setsid` is also
        # the exact spelling SKILL.md names for escaping the watchdog.
        "setsid", "ionice", "flock", "watch", "parallel", "taskset", "chrt",
        "unbuffer", "caffeinate", "doas",
    }
)

#: Wrappers that take positional operands before the command: `flock LOCK cmd`.
WRAPPER_POSITIONALS = {"flock": 1}

#: Language interpreters. These run arbitrary source, so they are never read
#: tier regardless of arguments -- a script file's contents are as opaque to a
#: lexical scan as a -c string is.
INTERPRETERS = frozenset(
    {
        "python", "python2", "python3", "perl", "ruby", "php", "lua", "tclsh",
        "node", "nodejs", "Rscript", "julia", "octave", "matlab", "awk", "gawk",
    }
)

#: Shells. Bare `bash script.sh` is still arbitrary execution, but `-c` is the
#: form that smuggles a command string past every name-based check.
SHELLS = frozenset({"bash", "sh", "zsh", "ksh", "dash", "csh", "tcsh", "fish"})

#: Editors that write a file in place. The skill's rule is pull / edit locally
#: / push; `ex -c '%s/a/b/' -c wq file` is that rule broken in one line, and
#: it reached read tier.
EDITORS = frozenset({"vi", "vim", "nvim", "ex", "ed", "nano", "emacs", "pico"})

#: Builtins that execute a string assembled at run time.
EVAL_LIKE = frozenset({"eval", "source", "."})

#: Scheduler commands that spawn an arbitrary process on a compute node.
EXEC_LAUNCHERS = frozenset({"srun", "mpirun", "mpiexec", "ibrun", "aprun"})

#: Build drivers: a Makefile recipe is arbitrary shell this scan cannot see.
BUILD_DRIVERS = frozenset({"make", "gmake", "cmake", "ninja", "scons"})

#: Commands that move bytes across the network. `curl | bash` pulls in code;
#: any of them can push data off a machine holding unpublished results.
NETWORK_CMDS = frozenset(
    {
        "curl", "wget", "scp", "sftp", "rsync", "rclone", "aws", "gsutil",
        "nc", "ncat", "telnet",
        # A hop to another node runs a command string this scan never sees.
        "ssh", "slogin", "mosh",
    }
)

#: git subcommands that only read, in every form they take.
GIT_READ = frozenset(
    {
        "status", "log", "diff", "show", "rev-parse",
        "ls-files", "ls-remote", "describe", "blame", "shortlog",
        "cat-file", "rev-list",
    }
)

#: git subcommands that read in their bare/listing form but MUTATE otherwise:
#: `git config user.email x` writes, `git branch -D main` deletes a branch,
#: `git remote add` rewrites remotes. These sat in _GIT_READ, so every mutating
#: form of them reached read tier and was sent with no confirmation at all.
GIT_DUAL_MODE = frozenset({"config", "branch", "remote", "tag", "stash", "notes"})

#: Flags that make a dual-mode subcommand a pure read.
GIT_READ_ONLY_FLAGS = frozenset(
    {"-l", "--list", "--get", "--get-all", "--get-regexp", "-v", "-vv",
     "--verbose", "--show-origin", "--show-scope", "--contains",
     "--merged", "--no-merged", "--points-at", "-a", "--all", "--show-current"}
)

#: Wrapper flags that consume the FOLLOWING token as their value. Skipping the
#: flag but not its value leaves the value read as the command -- which is how
#: `xargs -n 1 rm /x` was classified as the command `1`.
#:
#: Keyed per wrapper because the same letter differs between them: `-i` takes a
#: value for xargs (the replace string) but takes none for env, where it means
#: "empty environment". Treating it as value-taking for env swallowed the verb
#: instead, turning `env -i rm -rf /x` into the command `/x`.
WRAPPER_VALUE_FLAGS = {
    "xargs": frozenset(
        {"-n", "-P", "-I", "-i", "-a", "-E", "-d", "-s", "-L", "-l",
         "--max-args", "--max-procs", "--replace", "--arg-file",
         "--delimiter", "--max-chars", "--max-lines"}
    ),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-p", "--prompt"}),
    "watch": frozenset({"-n", "--interval"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid"}),
    "taskset": frozenset({"-c", "--cpu-list", "-p", "--pid"}),
    "chrt": frozenset({"-p", "--pid"}),
    "flock": frozenset({"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"}),
    "parallel": frozenset({"-j", "--jobs", "-a", "--arg-file", "-S", "--sshlogin"}),
}


#: Commands that only read whatever their flags: shell builtins, and account
#: and scheduler queries checked to have no option that writes a file or runs
#: a program. Redirections are classified separately, so ``echo x > f`` is
#: still a write. A command that touches files and has options is not here:
#: it goes in :data:`READ_OPTIONS`, with the options it may carry.
READ_ANY = frozenset(
    {
        # shell builtins and structure
        "cd", "pushd", "popd", "echo", "printf", "true", "false", ":", "test",
        "[", "[[", "export", "set", "unset", "local", "declare", "readonly",
        "shift", "exit", "return", "break", "continue", "wait", "read", "let",
        "type", "sleep", "pwd", "basename", "dirname", "seq", "expr",
        "yes",  # prints forever; only a redirect could write, checked apart
        # system and account state
        "whoami", "id", "groups", "uptime", "nproc", "free", "df", "ps",
        "printenv", "which",
        # quota reports. showquota, where a site has it, wraps GPFS
        # mmlsquota: display options only, -u/-v limited to admins, its one
        # temp file in /tmp removed on exit.
        "showquota", "mmlsquota",
        # SLURM queries
        "squeue", "sacct", "sinfo", "sshare", "sprio", "sstat", "seff",
    }
)


def _f(*names: str) -> frozenset:
    return frozenset("--" + n for n in names)


@dataclass(frozen=True)
class ReadSpec:
    """The options a reading command may carry and still be a read.

    A command used to be a read whatever its flags, and three of them could
    write a file that way: ``sed -n 'w FILE'``, ``xxd -r IN OUT`` and
    ``sort -oFILE``. So the options are
    listed, and an option that is not listed makes the command unknown
    (refused below ``improvise``), like a command that is not listed.

    ``short`` are single letters that take no value; ``short_value`` letters
    take one, attached (``-n5``) or as the next token; ``short_optional``
    letters take one only when attached (``date -Iseconds``). ``long`` names
    are accepted with or without ``=value``; those in ``long_value`` may
    also take the next token. ``numeric`` allows ``-20``. ``exact`` tokens
    are accepted as written. More than ``max_operands`` file operands makes
    the command unknown (``xxd IN OUT`` writes OUT).
    """

    short: str = ""
    short_value: str = ""
    short_optional: str = ""
    long: frozenset = field(default_factory=frozenset)
    long_value: frozenset = field(default_factory=frozenset)
    numeric: bool = False
    exact: frozenset = field(default_factory=frozenset)
    max_operands: int | None = None


_SUM = ReadSpec(short="bctwz", long=_f(
    "binary", "check", "tag", "text", "zero", "ignore-missing", "quiet",
    "status", "strict", "warn"))
_ZCAT = ReadSpec(short="fqv")

#: Reading commands and the options each may carry. ``--help`` and
#: ``--version`` are accepted for all of them. Leave an option out unless it
#: can't write a file or run a program: left out, it costs a refusal that
#: names it; wrongly included, it is a write with no approval.
READ_OPTIONS = {
    "cat": ReadSpec(short="AbeEnstTuv", long=_f(
        "show-all", "number-nonblank", "show-ends", "number", "squeeze-blank",
        "show-tabs", "show-nonprinting")),
    "head": ReadSpec(short="qvz", short_value="nc", numeric=True, long=_f(
        "lines", "bytes", "quiet", "silent", "verbose", "zero-terminated")),
    "tail": ReadSpec(short="qvzfFr", short_value="ncs", numeric=True, long=_f(
        "lines", "bytes", "follow", "retry", "pid", "sleep-interval", "quiet",
        "silent", "verbose", "zero-terminated")),
    "wc": ReadSpec(short="clmwL", long=_f(
        "bytes", "chars", "lines", "words", "max-line-length", "files0-from",
        "total")),
    "cut": ReadSpec(short="nsz", short_value="bcdf", long=_f(
        "bytes", "characters", "delimiter", "fields", "complement",
        "only-delimited", "output-delimiter", "zero-terminated")),
    "tr": ReadSpec(short="cCdst", long=_f(
        "complement", "delete", "squeeze-repeats", "truncate-set1")),
    "paste": ReadSpec(short="sz", short_value="d", long=_f(
        "delimiters", "serial", "zero-terminated")),
    "join": ReadSpec(short="iz", short_value="aejotv12", long=_f(
        "ignore-case", "check-order", "nocheck-order", "header",
        "zero-terminated")),
    "column": ReadSpec(short="txnJLe", short_value="socNHRTWEOd", long=_f(
        "table", "separator", "output-separator", "columns", "table-columns",
        "table-right", "table-hide", "table-noheadings", "json", "fillrows",
        "output-width", "table-name", "keep-empty-lines")),
    "nl": ReadSpec(short="p", short_value="bdfhilnsvw", long=_f(
        "body-numbering", "section-delimiter", "footer-numbering",
        "header-numbering", "line-increment", "join-blank-lines",
        "number-format", "no-renumber", "number-separator",
        "starting-line-number", "number-width")),
    "fold": ReadSpec(short="bs", short_value="w", numeric=True, long=_f(
        "bytes", "spaces", "width")),
    "rev": ReadSpec(short="0", long=_f("zero")),
    "tac": ReadSpec(short="br", short_value="s", long=_f(
        "before", "regex", "separator")),
    "od": ReadSpec(short="bcdDfFhHiIlLoOsvxX", short_value="AjNStw", long=_f(
        "address-radix", "endian", "format", "output-duplicates",
        "read-bytes", "skip-bytes", "strings", "traditional", "width")),
    "hexdump": ReadSpec(short="bcCdovx", short_value="nsLe", long=_f(
        "canonical", "length", "skip", "no-squeezing", "format",
        "one-byte-octal", "one-byte-char", "two-bytes-decimal",
        "two-bytes-octal", "two-bytes-hex", "color")),
    # `xxd -r IN OUT` and `xxd IN OUT` both write OUT.
    "xxd": ReadSpec(short="abCdeEiu", short_value="cglnos", max_operands=1,
                    exact=frozenset({"-p", "-ps", "-plain", "-postscript"})),
    "strings": ReadSpec(short="afowd", short_value="nteTs", numeric=True, long=_f(
        "all", "print-file-name", "bytes", "radix", "encoding",
        "include-all-whitespace", "data", "output-separator")),
    # `file -C` compiles a magic file, which writes one.
    "file": ReadSpec(short="bdEhikLNnrsSzZ0", short_value="eFP", long=_f(
        "brief", "mime", "mime-type", "mime-encoding", "dereference",
        "no-dereference", "uncompress", "keep-going", "separator",
        "extension", "exclude", "print0", "raw", "special-files")),
    "stat": ReadSpec(short="fLt", short_value="c", long=_f(
        "dereference", "file-system", "format", "printf", "terse", "cached")),
    "readlink": ReadSpec(short="fenmqsvz", long=_f(
        "canonicalize", "canonicalize-existing", "canonicalize-missing",
        "no-newline", "quiet", "silent", "verbose", "zero")),
    "realpath": ReadSpec(short="eEmLPqsz", long=_f(
        "canonicalize-existing", "canonicalize-missing", "logical",
        "physical", "quiet", "relative-to", "relative-base", "strip",
        "no-symlinks", "zero")),
    "diff": ReadSpec(short="qsicuyEZbwBaTtdNrpPHlne", short_value="UCWIxXDFSL",
                     numeric=True, long=_f(
        "brief", "report-identical-files", "context", "unified",
        "side-by-side", "ignore-case", "ignore-tab-expansion",
        "ignore-trailing-space", "ignore-space-change", "ignore-all-space",
        "ignore-blank-lines", "ignore-matching-lines", "text",
        "strip-trailing-cr", "recursive", "new-file", "exclude",
        "exclude-from", "width", "suppress-common-lines", "minimal", "color",
        "label", "left-column", "expand-tabs", "no-dereference", "normal",
        "show-c-function", "show-function-line", "speed-large-files",
        "from-file", "to-file", "initial-tab")),
    "cmp": ReadSpec(short="bls", short_value="in", long=_f(
        "print-bytes", "ignore-initial", "verbose", "bytes", "quiet", "silent")),
    "comm": ReadSpec(short="123z", long=_f(
        "check-order", "nocheck-order", "output-delimiter", "total",
        "zero-terminated")),
    "zcat": _ZCAT, "gzcat": _ZCAT, "bzcat": _ZCAT, "xzcat": _ZCAT,
    "md5sum": _SUM, "sha1sum": _SUM, "sha256sum": _SUM,
    "cksum": ReadSpec(short="cbtwz", short_value="al", long=_f(
        "algorithm", "base64", "check", "length", "untagged", "tag", "zero",
        "ignore-missing", "quiet", "status", "strict", "warn", "raw")),
    "jq": ReadSpec(short="nrjaSCMceRsh", short_value="fL", long=_f(
        "null-input", "raw-input", "slurp", "raw-output", "join-output",
        "ascii-output", "sort-keys", "color-output", "monochrome-output",
        "compact-output", "tab", "indent", "unbuffered", "stream", "seq",
        "exit-status", "arg", "argjson", "slurpfile", "rawfile", "args",
        "jsonargs", "from-file")),
    "numfmt": ReadSpec(short="z", short_value="d", long=_f(
        "from", "to", "from-unit", "to-unit", "suffix", "padding", "round",
        "format", "field", "delimiter", "grouping", "header", "invalid",
        "zero-terminated", "debug")),
    # netCDF: ncdump prints and cannot write a file
    "ncdump": ReadSpec(short="hckstx", short_value="vbfnlpigL"),
    # `hostname NAME` sets the name; `date -s` sets the clock.
    "hostname": ReadSpec(short="sfdiIAy", max_operands=0, long=_f(
        "short", "fqdn", "long", "domain", "ip-address", "all-ip-addresses",
        "all-fqdns", "yp", "nis")),
    "date": ReadSpec(short="uR", short_value="drf", short_optional="I",
                     long_value=_f("date", "reference", "file"), long=_f(
        "date", "utc", "universal", "iso-8601", "rfc-3339", "rfc-email",
        "reference", "file", "debug")),
    # `sort -o FILE` writes and is handled by ARG_RULES; `--compress-program`
    # runs a program and `-T` writes temporary files where it points.
    "sort": ReadSpec(short="bdfgiMhnRrVcCmsuz", short_value="kSto", long=_f(
        "ignore-leading-blanks", "dictionary-order", "ignore-case",
        "general-numeric-sort", "ignore-nonprinting", "month-sort",
        "human-numeric-sort", "numeric-sort", "random-sort", "reverse",
        "version-sort", "sort", "check", "merge", "stable", "unique",
        "zero-terminated", "key", "field-separator", "buffer-size",
        "parallel", "batch-size", "files0-from", "output")),
    "uniq": ReadSpec(short="cdDiuz", short_value="fsw", numeric=True, long=_f(
        "count", "repeated", "all-repeated", "group", "ignore-case", "unique",
        "zero-terminated", "skip-fields", "skip-chars", "check-chars")),
}

#: Every command that is a read by name whatever its operands. `sort` and
#: `uniq` have a writing form too; ARG_RULES, below, decides those.
READ_COMMANDS = READ_ANY | (frozenset(READ_OPTIONS) - {"sort", "uniq"})

_NUMERIC_OPT = re.compile(r"-\d+")


def split_options(tokens: list[str], short_value: str = "",
                  long_value=frozenset(), short_optional: str = "",
                  numeric: bool = True):
    """``(options, operands)`` of a command line, GNU style.

    ``options`` is a list of ``(name, value)``: ``("-o", "f")`` for ``-o f``
    and for the attached ``-of``, ``("--output", "f")`` for ``--output=f``
    and for ``--output f`` when the name is in ``long_value``, ``("-#", "20")``
    for ``-20``. A cluster ``-abc`` is three options. Everything after ``--``
    is an operand. Reading only the spaced form is what let ``sort -oFILE``
    and ``cp -tDIR`` through. With ``numeric=False`` a token such as ``-12``
    is a cluster of letters (``comm -12``, ``join -1 1``), not ``-NUM``.
    """
    opts: list[tuple[str, str | None]] = []
    operands: list[str] = []
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            operands.extend(tokens[i + 1:])
            break
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if eq:
                opts.append((name, value))
            elif name in long_value and i + 1 < len(tokens):
                opts.append((name, tokens[i + 1]))
                i += 1
            else:
                opts.append((name, None))
        elif tok.startswith("-") and len(tok) > 1:
            if numeric and _NUMERIC_OPT.fullmatch(tok):
                opts.append(("-#", tok[1:]))
            else:
                j = 1
                while j < len(tok):
                    ch = tok[j]
                    if ch in short_value:
                        rest = tok[j + 1:]
                        if rest:
                            opts.append(("-" + ch, rest))
                        elif i + 1 < len(tokens):
                            opts.append(("-" + ch, tokens[i + 1]))
                            i += 1
                        else:
                            opts.append(("-" + ch, None))
                        break
                    if ch in short_optional:
                        opts.append(("-" + ch, tok[j + 1:] or None))
                        break
                    opts.append(("-" + ch, None))
                    j += 1
        else:
            operands.append(tok)
        i += 1
    return opts, operands


def split_by_flags(tokens: list[str], value_flags):
    """:func:`split_options` for a rule that names its value-taking flags as
    ``-x`` and ``--long`` strings."""
    return split_options(
        tokens,
        short_value="".join(f[1] for f in value_flags if len(f) == 2),
        long_value=frozenset(f for f in value_flags if f.startswith("--")))


def unknown_option(cmd: str, tokens: list[str]) -> str | None:
    """Why ``cmd`` (a key of READ_OPTIONS) is not a known read in this form,
    or None if every option it carries is listed."""
    spec = READ_OPTIONS[cmd]
    rest = [t for t in tokens[1:] if t not in spec.exact]
    opts, operands = split_options(
        [cmd] + rest, spec.short_value, spec.long_value, spec.short_optional,
        numeric=spec.numeric)
    for name, _ in opts:
        if name in ("--help", "--version"):
            continue
        if name == "-#":
            if spec.numeric:
                continue
        elif name.startswith("--"):
            if name in spec.long:
                continue
        elif name[1] in spec.short + spec.short_value + spec.short_optional:
            continue
        shown = "a numeric option" if name == "-#" else f"the option {name}"
        return (f"{shown} of {cmd} is not one this guard knows to be "
                "read-only")
    if spec.max_operands is not None and len(operands) > spec.max_operands:
        return (f"{cmd} with {len(operands)} operand(s) may write or change "
                "state in this form")
    return None


@dataclass(frozen=True)
class ArgRule:
    """A command that reads in one form and writes in another.

    It writes when any of ``output_flags`` is present, or when it has more
    than ``max_inputs`` file operands (the extra one is an output file).
    ``value_flags`` take a value, attached or as the next token, so that
    value is not counted as a file. A flag this table does not know is
    assumed to take no value; if it does, its value is counted as a file
    and the command is called a write. That error is in the safe direction.
    """

    value_flags: frozenset = field(default_factory=frozenset)
    output_flags: frozenset = field(default_factory=frozenset)
    max_inputs: int | None = None


ARG_RULES = {
    # `ncks -H -C -v T in.nc` prints; `ncks -v T in.nc out.nc` writes.
    "ncks": ArgRule(
        value_flags=frozenset({
            "-v", "--variable", "-d", "--dimension", "-g", "--grp", "-s",
            "--sng_fmt", "-t", "--thr_nbr", "-p", "--pth", "-l", "--lcl",
            "-L", "--dfl_lvl", "-b", "--fl_bnr", "-X", "--auxiliary",
            "--cnk_dmn", "--cnk_plc", "--cnk_map", "--ppc", "--fmt",
            "--hdr_pad", "-D", "--dbg_lvl",
        }),
        output_flags=frozenset({"-o", "--output", "--fl_out"}),
        max_inputs=1,
    ),
    # `sort -o FILE` writes FILE.
    "sort": ArgRule(
        value_flags=frozenset({"-k", "--key", "-t", "--field-separator",
                               "-S", "--buffer-size", "-T", "--temporary-directory"}),
        output_flags=frozenset({"-o", "--output"}),
    ),
    # `uniq IN OUT` writes OUT.
    "uniq": ArgRule(
        value_flags=frozenset({"-f", "--skip-fields", "-s", "--skip-chars",
                               "-w", "--check-chars"}),
        max_inputs=1,
    ),
}


def writes_by_args(cmd: str, tokens: list[str]) -> bool:
    """True if ``cmd`` (a key of ARG_RULES) writes, given its tokens."""
    rule = ARG_RULES[cmd]
    opts, operands = split_by_flags(tokens, rule.value_flags | rule.output_flags)
    if any(name in rule.output_flags for name, _ in opts):
        return True
    return rule.max_inputs is not None and len(operands) > rule.max_inputs


@dataclass(frozen=True)
class TargetRule:
    """Which arguments of a write command are the files it writes.

    ``operands`` picks from the non-flag arguments: ``"all"``, ``"last"``,
    ``"first"``, ``"all_but_first"`` (chmod's mode comes first) or
    ``"none"``. ``target_flags`` take a value that is itself a write target
    (``cp -t DIR``, ``sort -o FILE``); when one is given, the operands are
    sources and are not checked. ``cwd`` means the command also writes into
    the working directory (``tar -x``, ``split``, ``unzip`` without ``-d``,
    ``ln`` with one operand). ``stdout_flags`` send the output to stdout, so
    no file is written (``gzip -c``).

    hpctether/confine.py applies these. Every target must resolve inside a
    write root, or the command is never sent. A flag missing from
    ``value_flags`` has its value read as an operand, and so checked: the
    error is in the safe direction.
    """

    operands: str = "all"
    value_flags: frozenset = field(default_factory=frozenset)
    target_flags: frozenset = field(default_factory=frozenset)
    cwd: bool = False
    stdout_flags: frozenset = field(default_factory=frozenset)
    #: ``ln -s /x`` with one operand makes ``./x``. For every other "last"
    #: command a lone operand is itself the target (``ncatted ... f.nc``
    #: edits f.nc in place).
    single_is_cwd: bool = False


_COMPRESS = TargetRule(stdout_flags=frozenset({"-c", "--stdout", "--to-stdout"}))
_NCO = TargetRule(operands="last",
                  # ncatted/ncrename take `-a att,var,...` and `-v old,new`
                  value_flags=ARG_RULES["ncks"].value_flags | {"-a", "--attribute"},
                  target_flags=frozenset({"-o", "--output", "--fl_out"}))

#: Commands with no file target: they act on jobs and processes.
NO_FILE_TARGET = frozenset({"sbatch", "scancel", "scontrol", "kill", "pkill"})

WRITE_TARGETS = {
    # mv's own confinement lives in guard.py; this entry tells the
    # script-root rule which file it writes.
    "mv": TargetRule(operands="last", value_flags=frozenset({"-S", "--suffix"}),
                     target_flags=frozenset({"-t", "--target-directory"})),
    "cp": TargetRule(operands="last",
                     value_flags=frozenset({"-S", "--suffix"}),
                     target_flags=frozenset({"-t", "--target-directory"})),
    "install": TargetRule(operands="last",
                          value_flags=frozenset({"-m", "--mode", "-o", "--owner",
                                                 "-g", "--group", "-S", "--suffix"}),
                          target_flags=frozenset({"-t", "--target-directory"})),
    "ln": TargetRule(operands="last", single_is_cwd=True,
                     target_flags=frozenset({"-t", "--target-directory"})),
    "mkdir": TargetRule(value_flags=frozenset({"-m", "--mode"})),
    "touch": TargetRule(value_flags=frozenset({"-d", "--date", "-r", "--reference", "-t"})),
    "tee": TargetRule(),
    "mkfifo": TargetRule(value_flags=frozenset({"-m", "--mode"})),
    "chmod": TargetRule(operands="all_but_first"),
    "gzip": _COMPRESS, "gunzip": _COMPRESS, "bzip2": _COMPRESS,
    "bunzip2": _COMPRESS, "xz": _COMPRESS, "unxz": _COMPRESS,
    "zip": TargetRule(operands="first"),
    "unzip": TargetRule(operands="none", target_flags=frozenset({"-d"}), cwd=True,
                        stdout_flags=frozenset({"-p", "-l"})),
    "tar": TargetRule(value_flags=frozenset(), cwd=True),
    "split": TargetRule(value_flags=frozenset({"-l", "--lines", "-b", "--bytes",
                                               "-n", "--number", "-a"}), cwd=True),
    "csplit": TargetRule(value_flags=frozenset({"-f", "--prefix", "-n", "-b"}), cwd=True),
    "ncks": _NCO,
    "ncrcat": _NCO, "ncra": _NCO, "ncea": _NCO, "nces": _NCO, "ncecat": _NCO,
    "ncwa": _NCO, "ncbo": _NCO, "ncdiff": _NCO, "ncflint": _NCO, "ncpdq": _NCO,
    "ncap2": _NCO, "ncrename": _NCO, "ncatted": _NCO, "nccopy": _NCO,
    "cdo": TargetRule(operands="last"),
    "sort": TargetRule(operands="none", value_flags=ARG_RULES["sort"].value_flags,
                       target_flags=frozenset({"-o", "--output"})),
    "uniq": TargetRule(operands="last", value_flags=ARG_RULES["uniq"].value_flags),
}

# --- sed: a read only for program text the guard recognizes -----------------

_SED_SPEC = ReadSpec(short="nErsuz", short_value="el", long=_f(
    "quiet", "silent", "regexp-extended", "expression", "separate",
    "unbuffered", "null-data", "line-length", "posix", "debug", "sandbox"))


def _sed_delimited(text: str, i: int, delim: str, regex: bool) -> int | None:
    """Index just past the ``delim`` that closes the part starting at ``i``."""
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if regex and ch == "[" and delim != "[":
            # A bracket expression: the delimiter inside it is literal. A
            # class such as [:alpha:] has its own `]`, which does not end it.
            j = i + 1
            if j < n and text[j] == "^":
                j += 1
            if j < n and text[j] == "]":
                j += 1
            while j < n and text[j] != "]":
                if text[j] == "[" and j + 1 < n and text[j + 1] in ":.=":
                    k = text.find(text[j + 1] + "]", j + 2)
                    if k < 0:
                        return None
                    j = k + 2
                    continue
                j += 1
            i = j + 1
            continue
        if ch == delim:
            return i + 1
        if ch == "\n" and delim != "\n":
            return None
        i += 1
    return None


def sed_program_reads(text: str) -> bool:
    """True if every command in a sed program is one that only reads.

    Recognized: addresses, ``p d D n N g G h H x l P z = q Q``, ``s`` and ``y``
    with their flags, ``b t T`` and labels, ``{ }``, comments. Not recognized,
    so the program is not a read: ``w`` and ``W`` (write a file), ``e`` (run a
    command), the ``w`` and ``e`` flags of ``s``, ``r`` and ``R``, and the
    text commands ``a i c``. `sed -n 'w /x'` wrote outside every root at
    ``observe``. Anything this can't parse is
    not a read.
    """
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\n;{}":
            i += 1
        if i >= n:
            break
        for _ in range(2):  # up to two addresses
            if text[i].isdigit():
                while i < n and (text[i].isdigit() or text[i] == "~"):
                    i += 1
            elif text[i] == "$":
                i += 1
            elif text.startswith("\\$", i):
                # `sed -n "1p;\$p"`: inside double quotes the shell turns
                # `\$` into `$`, the last-line address.
                i += 2
            elif text[i] == "/":
                i = _sed_delimited(text, i + 1, "/", True)
            elif text[i] == "\\" and i + 1 < n:
                i = _sed_delimited(text, i + 2, text[i + 1], True)
            elif text[i] in "+~" and i + 1 < n and text[i + 1].isdigit():
                i += 1
                while i < n and text[i].isdigit():
                    i += 1
            else:
                break
            if i is None or i >= n:
                return False  # an address with no command
            while i < n and text[i] in "IM":
                i += 1
            if i < n and text[i] == ",":
                i += 1
                continue
            break
        while i < n and text[i] in " \t!":
            i += 1
        if i >= n:
            return False
        ch = text[i]
        if ch in "{}":
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch in "pdDnNgGhHxPz=":
            i += 1
        elif ch in "lqQ":
            i += 1
            while i < n and text[i] in " \t":
                i += 1
            while i < n and text[i].isdigit():
                i += 1
        elif ch in "btT:":
            i += 1
            while i < n and text[i] not in ";\n}":
                i += 1
        elif ch in "sy":
            if i + 1 >= n or text[i + 1] in "\\\n ":
                return False
            delim = text[i + 1]
            j = _sed_delimited(text, i + 2, delim, ch == "s")
            if j is None:
                return False
            j = _sed_delimited(text, j, delim, False)
            if j is None:
                return False
            i = j
            if ch == "s":
                while i < n and (text[i] in "gpiIMm" or text[i].isdigit()):
                    i += 1
        else:
            return False
        while i < n and text[i] in " \t":
            i += 1
        if i < n and text[i] not in ";\n}":
            return False
    return True


def sed_unknown(tokens: list[str]) -> str | None:
    """Why this ``sed`` is not a known read, or None. (``-i`` is refused
    before this is asked.)"""
    opts, operands = split_options(
        tokens, _SED_SPEC.short_value, long_value=frozenset({"--expression"}))
    programs = []
    for name, value in opts:
        if name in ("--help", "--version"):
            continue
        if name in ("-e", "--expression"):
            programs.append(value or "")
        elif name.startswith("--"):
            if name not in _SED_SPEC.long:
                return f"the option {name} of sed is not one this guard knows to be read-only"
        elif name == "-#" or name[1] not in _SED_SPEC.short + _SED_SPEC.short_value:
            return (f"the option {name} of sed is not one this guard knows to "
                    "be read-only (a program file, -f, can't be read from here)")
    if not programs:
        if not operands:
            return None
        programs.append(operands[0])
    for program in programs:
        if "__HPC_SUBST_" in program or not sed_program_reads(program):
            return ("this sed program is not one the guard recognizes as "
                    "read-only (it knows p, d, s, y, q, n, g, h, x, =, l, b, t "
                    "and addresses; w and e write files and run commands)")
    return None


# --- find: a read only for predicates the guard knows ----------------------

#: ``-fprint FILE`` and its kin write FILE; ``-ok`` runs a command like
#: ``-exec``. Neither was checked before.
FIND_PREDICATES = frozenset(
    "-" + p for p in (
        "maxdepth mindepth name iname path ipath wholename iwholename regex "
        "iregex regextype type xtype size mtime mmin newer atime amin ctime "
        "cmin anewer cnewer user group uid gid perm empty print print0 printf "
        "ls prune o or a and not links inum samefile readable writable "
        "executable depth d xdev mount daystart follow L H P quit true false "
        "noleaf nouser nogroup lname ilname used fstype O0 O1 O2 O3"
    ).split()
)
FIND_RUNS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})
_FIND_NEWER = re.compile(r"-newer[aBcm][aBcmt]")
_FIND_NUMBER = re.compile(r"[-+]\d")


def find_unknown(tokens: list[str]) -> str | None:
    """The first ``find`` predicate this guard doesn't know, or None."""
    for tok in tokens[1:]:
        if not tok.startswith("-") or tok in FIND_PREDICATES:
            continue
        if _FIND_NEWER.fullmatch(tok) or _FIND_NUMBER.match(tok):
            continue
        return tok
    return None


# --- write commands: forms that delete or run a program ---------------------

#: Compressors replace their input: `gzip f` leaves f.gz and no f. The
#: content survives, but the name a running job reads is gone, and this
#: toolkit removes nothing.
COMPRESSORS = frozenset({"gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz"})

_TAR_SHORT = frozenset("cxtrzjJvfCpkOhmoaPS")
_TAR_LONG = _f(
    "create", "extract", "get", "list", "append", "file", "directory", "gzip",
    "gunzip", "bzip2", "xz", "zstd", "verbose", "keep-old-files",
    "skip-old-files", "overwrite", "no-same-owner", "no-same-permissions",
    "preserve-permissions", "strip-components", "exclude", "wildcards",
    "no-wildcards", "totals", "to-stdout", "dereference", "touch",
    "auto-compress", "numeric-owner", "one-file-system", "files-from",
    "no-recursion", "help", "version")


def write_form_refusal(cmd: str, tokens: list[str]) -> tuple[str, str] | None:
    """``(kind, why)`` for a write command spelled in a form that deletes
    (kind ``"forbidden"``) or runs a program (kind ``"unknown"``), else None."""
    opts, _ = split_options(tokens)
    names = [name for name, _ in opts]
    if cmd in COMPRESSORS:
        if not {"-k", "--keep", "-c", "--stdout", "--to-stdout", "-t", "--test",
                "-l", "--list"} & set(names):
            return ("forbidden",
                    f"{cmd} replaces its input file, which removes the original "
                    "name; this toolkit removes nothing on the cluster. Add -k "
                    "(keep the input) or write to stdout with -c")
    elif cmd == "tar":
        rest = tokens[1:]
        if rest and not rest[0].startswith("-"):
            rest = ["-" + rest[0]] + rest[1:]  # old style: `tar xzf a.tar`
        for name, _ in split_options(["tar"] + rest, "fC",
                                     _f("file", "directory", "exclude",
                                        "strip-components", "files-from"))[0]:
            if name in ("--remove-files", "--delete", "--unlink-first",
                        "--recursive-unlink", "-U", "-W"):
                return ("forbidden", f"tar {name} deletes files; this toolkit "
                        "removes nothing on the cluster")
            ok = (name in _TAR_LONG if name.startswith("--")
                  else name[1] in _TAR_SHORT or name == "-#")
            if not ok:
                return ("unknown", f"the option {name} of tar is not one this "
                        "guard knows (some tar options run a program)")
    elif cmd == "zip":
        if "-m" in names or "--move" in names:
            return ("forbidden", "zip -m deletes the files it archives; this "
                    "toolkit removes nothing on the cluster")
        if "-T" in names or "--test" in names or "--unzip-command" in names:
            return ("unknown", "zip -T can run a program of the payload's choosing")
    elif cmd == "split":
        if "--filter" in names:
            return ("unknown", "split --filter runs a command this scan cannot read")
    elif cmd == "install":
        if "--strip-program" in names:
            return ("unknown", "install --strip-program runs a program this "
                    "scan cannot read")
    return None



#: Files that are code, by extension (compared lowercased). Writing one into a
#: script root would make it "trusted" without a commit: a trusted .py imports
#: its neighbours and can shell out to them. Code reaches a script root only
#: through git (plan §4.1).
CODE_SUFFIXES = frozenset({
    ".py", ".pyc", ".pyo", ".pyx", ".pxd", ".pyi", ".pth", ".so", ".ipynb",
    ".sh", ".bash", ".csh", ".tcsh", ".zsh", ".ksh", ".fish",
    ".pl", ".pm", ".rb", ".r", ".jl", ".lua", ".js", ".mjs",
})

#: Commands that unpack an archive or stream into files whose names the
#: payload text doesn't show, so they are refused into a script root.
UNPACKERS = frozenset({"tar", "unzip", "gunzip", "bunzip2", "unxz", "split", "csplit"})

#: Commands that never put code in a root: a directory, a mode change.
NO_CODE_WRITE = frozenset({"mkdir", "chmod", "mkfifo"})


#: Environment variables that decide what code a program loads or what a
#: name on its command line resolves to. A payload that sets one can make a
#: trusted script import, preload or shell out to code from scratch:
#: `PYTHONPATH=$HPC_SCRATCH python $ROOT/my-script.py` runs the agent's
#: `yaml.py` when my-script.py imports yaml. So a payload that mentions one, other
#: than as `$VAR`, runs no trusted script (guard.classify). The one exception
#: is the site's own prelude form, `export PATH="$HPC_PYTHON_BIN:$PATH"`.
LOADER_ENV = frozenset({
    "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
    "PYTHONINSPECT", "PYTHONEXECUTABLE", "PYTHONPLATLIBDIR",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "BASH_ENV", "ENV",
    "PERL5LIB", "PERL5OPT", "RUBYLIB", "RUBYOPT", "NODE_OPTIONS",
})
