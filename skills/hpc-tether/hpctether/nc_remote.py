"""The program hpc-nc runs on the cluster, and the payload that carries it.

``REMOTE`` is fixed toolkit code, reviewed here and sent verbatim on stdin
with the request as JSON. It is never written by an agent, which is why
hpc-nc may run it at profile ``observe``: the same footing as hpc-probe's
fixed ``srun`` payload. It only reads. It opens one or two netCDF files,
reads at most ``cap`` bytes of variable data (checked from shapes before
anything is read), and prints exactly one JSON line.

It knows nothing about any model. Variable, dimension and weight names all
come from the caller. Needs ``numpy`` and ``netCDF4`` in the cluster Python
(the site's ``python_bin`` role, else ``python3``); ``cftime``, if present,
decodes CF time coordinates in a slice.
"""

from __future__ import annotations

import json

#: Delimiter of the heredoc that carries REMOTE. A request containing it is
#: refused rather than quoted.
DELIM = "__HPC_NC__"

REMOTE = r'''
import json, math, sys

A = json.loads(ARGS_JSON)
CAP = int(A["cap"])
CHUNK = int(A.get("chunk", 32 * 1024 * 1024))  # the tests make it small


def emit(obj):
    print(json.dumps(obj, default=str))
    sys.exit(0)


def fail(code, message, **extra):
    extra.update(ok=False, error=code, message=message)
    emit(extra)


try:
    import numpy as np
    import netCDF4
except Exception as exc:
    fail("missing_libs", "%s: %s" % (type(exc).__name__, exc), python=sys.executable)


def plain(v):
    if isinstance(v, np.ndarray):
        v = v.tolist()
    elif isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, str) and len(v) > 300:
        return v[:300] + "..."
    if isinstance(v, list) and len(v) > 20:
        return v[:20] + ["..."]
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    return v


def attrs(obj):
    return {k: plain(obj.getncattr(k)) for k in obj.ncattrs()}


def open_ds(path):
    try:
        return netCDF4.Dataset(path, "r")
    except FileNotFoundError:
        fail("not_found", "no such file: %s" % path)
    except Exception as exc:
        fail("unreadable", "%s: %s" % (path, exc))


def get_var(ds, name):
    if name not in ds.variables:
        names = list(ds.variables)
        fail("no_variable", "no variable %r; it has: %s%s" % (
            name, ", ".join(names[:80]), " ..." if len(names) > 80 else ""))
    return ds.variables[name]


def size_of(v):
    """Bytes per value, for the read cap. A variable-length string has no
    fixed size; count it as 64 bytes, an overestimate on purpose."""
    return getattr(v.dtype, "itemsize", None) or 64


def numeric(v):
    return v.dtype.kind in "iufb" if hasattr(v.dtype, "kind") else False


def selection(v, specs):
    """Index slices for v from [dim, start, stop|None] specs (stop inclusive)."""
    chosen = {}
    for dim, start, stop in specs:
        if dim not in v.dimensions:
            fail("bad_dim", "%s has no dimension %r (it has: %s)" % (
                v.name, dim, ", ".join(v.dimensions) or "none, it is a scalar"))
        n = v.shape[v.dimensions.index(dim)]

        def norm(i):
            j = i + n if i < 0 else i
            if j < 0 or j >= n:
                fail("bad_index", "index %d is outside %s (size %d)" % (i, dim, n))
            return j

        a = norm(start)
        b = norm(stop) if stop is not None else a
        if b < a:
            fail("bad_index", "%s: stop %d comes before start %d" % (dim, stop, start))
        chosen[dim] = slice(a, b + 1)
    return tuple(chosen.get(d, slice(0, v.shape[i])) for i, d in enumerate(v.dimensions))


def count_of(sl):
    n = 1
    for s in sl:
        n *= s.stop - s.start
    return n


def check_cap(nbytes, what):
    if nbytes > CAP:
        fail("too_large", "%s would read %d bytes; the cap is %d" % (what, nbytes, CAP),
             bytes=nbytes, cap=CAP)


def read(v, sl):
    x = v[sl] if sl else v[...]
    return np.ma.asarray(x)


def chunks(v, sl):
    """Sub-selections of sl along its first dimension, each about CHUNK bytes."""
    if not sl:
        yield sl, 0
        return
    row = max(1, count_of(sl[1:]) * size_of(v))
    step = max(1, CHUNK // row)
    first = sl[0]
    for a in range(first.start, first.stop, step):
        b = min(first.stop, a + step)
        yield (slice(a, b),) + tuple(sl[1:]), a - first.start


def weights_for(v, sl, wname, ds):
    w = get_var(ds, wname)
    if not numeric(w):
        fail("bad_weights", "weights %r are not numeric" % wname)
    if not set(w.dimensions) <= set(v.dimensions):
        fail("bad_weights", "weights %s%s use a dimension %s%s lacks" % (
            wname, tuple(w.dimensions), v.name, tuple(v.dimensions)))
    wsl = tuple(sl[v.dimensions.index(d)] for d in w.dimensions)
    # The cap covers the variable and the weights together, and is checked
    # before either is read: weights shaped like the variable used to be
    # read in full first.
    check_cap(count_of(sl) * size_of(v) + count_of(wsl) * 8, "these statistics")
    wdata = np.ma.filled(read(w, wsl).astype("f8"), 0.0)
    order = [d for d in v.dimensions if d in w.dimensions]
    wdata = np.transpose(wdata, [w.dimensions.index(d) for d in order])
    shape = [wdata.shape[order.index(d)] if d in w.dimensions else 1 for d in v.dimensions]
    return wdata.reshape(shape), wsl


def op_header():
    ds = open_ds(A["path"])
    only = A.get("vars") or []
    for name in only:
        get_var(ds, name)
    out = {
        "ok": True, "path": A["path"], "format": ds.data_model,
        "dims": [{"name": n, "size": len(d), "unlimited": d.isunlimited()}
                 for n, d in ds.dimensions.items()],
        "vars": [{"name": n, "dims": list(v.dimensions), "shape": list(v.shape),
                  "dtype": str(v.dtype), "attrs": attrs(v)}
                 for n, v in ds.variables.items() if not only or n in only],
        "global_attrs": {} if only else attrs(ds),
        "groups": list(ds.groups),
    }
    emit(out)


def op_stats():
    ds = open_ds(A["path"])
    v = get_var(ds, A["var"])
    if not numeric(v):
        fail("not_numeric", "%s is %s, not numeric" % (v.name, v.dtype))
    sl = selection(v, A.get("dims") or [])
    n = count_of(sl)
    nbytes = n * size_of(v)
    wfull = None
    if A.get("weights"):
        wfull, wsl = weights_for(v, sl, A["weights"], ds)
        nbytes += count_of(wsl) * 8
    check_cap(nbytes, "these statistics")
    cnt = 0; s = 0.0; ss = 0.0; lo = None; hi = None
    nan = 0; inf = 0; fill = 0; sw = 0.0; swx = 0.0
    for sub, offset in chunks(v, sl):
        x = read(v, sub)
        mask = np.ma.getmaskarray(x)
        data = np.ma.getdata(x).astype("f8")
        valid = ~mask
        fill += int(mask.sum())
        nan += int((np.isnan(data) & valid).sum())
        inf += int((np.isinf(data) & valid).sum())
        good = valid & np.isfinite(data)
        g = data[good]
        if g.size:
            cnt += int(g.size); s += float(g.sum()); ss += float((g * g).sum())
            lo = float(g.min()) if lo is None else min(lo, float(g.min()))
            hi = float(g.max()) if hi is None else max(hi, float(g.max()))
        if wfull is not None:
            # wfull is laid out on v's dimensions, size 1 where the weights
            # lack one. Along the chunked first dimension, take this chunk's
            # rows if the weights vary there; otherwise broadcast.
            w = wfull
            if sl and wfull.shape[0] != 1:
                w = wfull[offset:offset + data.shape[0]]
            w = np.broadcast_to(w, data.shape)
            sw += float(w[good].sum()); swx += float((w[good] * data[good]).sum())
    mean = s / cnt if cnt else None
    out = {
        "ok": True, "path": A["path"], "var": v.name, "units": plain(getattr(v, "units", None)),
        "dims": list(v.dimensions), "selection": [[s_.start, s_.stop - 1] for s_ in sl],
        "values": n, "bytes_read": nbytes, "count": cnt, "min": lo, "max": hi,
        "mean": mean, "mean_kind": "unweighted",
        "std": math.sqrt(max(ss / cnt - mean * mean, 0.0)) if cnt else None,
        "nan": nan, "inf": inf, "fill": fill,
    }
    if wfull is not None:
        out["weights"] = A["weights"]
        out["weighted_mean"] = swx / sw if sw else None
    emit(out)


def coord_values(ds, dim, s):
    if dim not in ds.variables or ds.variables[dim].dimensions != (dim,):
        return None
    c = ds.variables[dim]
    raw = read(c, (s,))
    vals = np.ma.filled(raw.astype("f8"), np.nan) if numeric(c) else np.ma.getdata(raw)
    out = {"name": dim, "units": plain(getattr(c, "units", None)), "values": [plain(x) for x in vals.tolist()]}
    units = getattr(c, "units", "")
    if isinstance(units, str) and " since " in units:
        try:
            import cftime  # noqa: F401
            dates = netCDF4.num2date(vals, units, getattr(c, "calendar", "standard"),
                                     only_use_cftime_datetimes=True)
            out["decoded"] = [str(d) for d in np.atleast_1d(dates)]
        except Exception:
            pass
    return out


def op_slice():
    ds = open_ds(A["path"])
    v = get_var(ds, A["var"])
    sl = selection(v, A.get("dims") or [])
    n = count_of(sl)
    if n > int(A["limit"]):
        fail("too_many_values", "the selection holds %d values; the limit is %d. Narrow it "
             "with -d" % (n, int(A["limit"])), values=n, limit=int(A["limit"]))
    check_cap(n * size_of(v), "this slice")
    x = read(v, sl)
    if numeric(v):
        data = np.ma.getdata(x).astype("f8")
        cells = [None if m else (str(d) if not math.isfinite(d) else d)
                 for d, m in zip(data.ravel().tolist(), np.ma.getmaskarray(x).ravel().tolist())]
    else:
        cells = [plain(c) for c in np.ma.getdata(x).ravel().tolist()]
    emit({"ok": True, "path": A["path"], "var": v.name, "units": plain(getattr(v, "units", None)),
          "dims": list(v.dimensions), "shape": [s_.stop - s_.start for s_ in sl],
          "selection": [[s_.start, s_.stop - 1] for s_ in sl], "values": cells,
          "coords": [c for c in (coord_values(ds, d, s_) for d, s_ in zip(v.dimensions, sl)) if c]})


def op_diff():
    a = open_ds(A["path"]); b = open_ds(A["path_b"])
    out = {"ok": True, "path": A["path"], "path_b": A["path_b"]}
    da = {n: len(d) for n, d in a.dimensions.items()}
    db = {n: len(d) for n, d in b.dimensions.items()}
    out["dims_only_a"] = sorted(set(da) - set(db))
    out["dims_only_b"] = sorted(set(db) - set(da))
    out["dims_resized"] = {k: [da[k], db[k]] for k in sorted(set(da) & set(db)) if da[k] != db[k]}
    va, vb = set(a.variables), set(b.variables)
    out["vars_only_a"] = sorted(va - vb)
    out["vars_only_b"] = sorted(vb - va)
    ga, gb = attrs(a), attrs(b)
    out["global_attrs_changed"] = sorted(k for k in set(ga) | set(gb) if ga.get(k) != gb.get(k))
    shared = sorted(va & vb)
    wanted = A.get("vars") or []
    for name in wanted:
        get_var(a, name); get_var(b, name)
    names = wanted or shared
    changed_meta, compare = {}, []
    for name in names:
        x, y = a.variables[name], b.variables[name]
        notes = []
        if x.dimensions != y.dimensions: notes.append("dims %s -> %s" % (x.dimensions, y.dimensions))
        if x.shape != y.shape: notes.append("shape %s -> %s" % (x.shape, y.shape))
        if x.dtype != y.dtype: notes.append("dtype %s -> %s" % (x.dtype, y.dtype))
        xa, ya = attrs(x), attrs(y)
        diff_attrs = sorted(k for k in set(xa) | set(ya) if xa.get(k) != ya.get(k))
        if diff_attrs: notes.append("attrs differ: " + ", ".join(diff_attrs))
        if notes: changed_meta[name] = notes
        if x.shape == y.shape and numeric(x) and numeric(y):
            compare.append(name)
    out["vars_changed_meta"] = changed_meta
    total = sum(2 * count_of(selection(a.variables[n], [])) * size_of(a.variables[n])
                for n in compare)
    if total > CAP:
        if wanted:
            check_cap(total, "comparing these variables")
        out["data"] = None
        out["data_skipped"] = ("comparing the data of %d variables would read %d bytes; the "
                               "cap is %d. Name the variables to compare with --var" % (len(compare), total, CAP))
        emit(out)
    data = {}
    for name in compare:
        x, y = a.variables[name], b.variables[name]
        sl = selection(x, [])
        worst, where, ndiff = 0.0, None, 0
        for sub, offset in chunks(x, sl):
            p, q = read(x, sub), read(y, sub)
            pm, qm = np.ma.getmaskarray(p), np.ma.getmaskarray(q)
            pd, qd = np.ma.getdata(p).astype("f8"), np.ma.getdata(q).astype("f8")
            both_nan = np.isnan(pd) & np.isnan(qd)
            differ = (pm != qm) | (~pm & ~qm & ~both_nan & (pd != qd))
            ndiff += int(differ.sum())
            d = np.where(~pm & ~qm & np.isfinite(pd) & np.isfinite(qd), np.abs(pd - qd), 0.0)
            if d.size and float(d.max()) > worst:
                worst = float(d.max())
                idx = list(np.unravel_index(int(d.argmax()), d.shape))
                if idx:
                    idx[0] += sl[0].start + offset
                where = [int(i) for i in idx]
        if ndiff:
            data[name] = {"differing": ndiff, "max_abs_diff": worst, "at": where}
    out["data"] = data
    out["data_compared"] = len(compare)
    emit(out)


{"header": op_header, "stats": op_stats, "slice": op_slice, "diff": op_diff}[A["op"]]()
'''


def payload(request: dict) -> str:
    """The bash payload that runs REMOTE on ``request`` with the site's Python.

    The first line is a no-op naming the request, so the audit log's one-line
    ``detail`` says what was asked.
    """
    text = json.dumps(request, sort_keys=True)
    if DELIM in text:
        raise ValueError("request text contains the payload delimiter")
    label = f"{request['op']} {request['path']}"
    if request.get("var"):
        label += f" {request['var']}"
    return (
        f": hpc-nc {_quote(label)}\n"
        'if [ -n "${HPC_PYTHON_BIN:-}" ] && [ -x "$HPC_PYTHON_BIN/python" ]; then\n'
        '  hpc_py="$HPC_PYTHON_BIN/python"\n'
        "else\n"
        "  hpc_py=python3\n"
        "fi\n"
        f"\"$hpc_py\" - <<'{DELIM}'\n"
        f"ARGS_JSON = {json.dumps(text)}\n"
        f"{REMOTE.strip()}\n"
        f"{DELIM}\n"
    )


def header_fallback(path: str) -> str:
    """``ncdump -h`` for a cluster without the Python libraries."""
    return f": hpc-nc header fallback\nncdump -h -- {_quote(path)}\n"


def _quote(text: str) -> str:
    import shlex
    return shlex.quote(text)
