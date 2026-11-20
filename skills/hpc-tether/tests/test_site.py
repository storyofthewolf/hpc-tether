"""Site-file location: user config outside the package, templates inside it."""

from pathlib import Path

import pytest

from hpctether import site


def test_default_sites_dir_is_outside_the_package(monkeypatch):
    # An installed plugin is replaced on update; a site file inside it would
    # be lost. The default must be the user's config directory.
    monkeypatch.delenv("HPC_TETHER_SITES", raising=False)
    assert site.sites_dir() == Path.home() / ".config" / "hpc-tether" / "sites"
    package = site.templates_dir().parent
    assert package not in site.sites_dir().parents


def test_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HPC_TETHER_SITES", str(tmp_path))
    assert site.sites_dir() == tmp_path


@pytest.mark.parametrize("name", ["example"])
def test_shipped_templates_load(monkeypatch, name):
    # Placeholders must still pass validation, so a copied template works
    # once the user fills it in rather than failing on structure.
    monkeypatch.setenv("HPC_TETHER_SITES", str(site.templates_dir()))
    loaded = site.load(name)
    assert loaded.scheduler == "slurm"
    assert "scratch" in loaded.paths
    assert loaded.warnings == ()        # templates use the current layout


def test_templates_carry_no_personal_ids():
    # Every user directory in a shipped template is a placeholder. Checked
    # by shape, so the test itself names no real user.
    import re
    for path in site.templates_dir().glob("*.toml"):
        text = path.read_text()
        dirs = re.findall(r"/(?:home|scratch)/([A-Za-z0-9_]+)", text)
        assert dirs and set(dirs) <= {"yourusername", "YOU"}, (path.name, set(dirs))
        assert re.search(r'^user = "yourusername"', text, re.M), path.name


def test_empty_config_points_at_templates(monkeypatch, tmp_path):
    monkeypatch.setenv("HPC_TETHER_SITES", str(tmp_path))
    monkeypatch.delenv("HPC_TETHER_SITE", raising=False)
    with pytest.raises(site.SiteError) as info:
        site.load()
    assert str(site.templates_dir()) in str(info.value)
    assert str(tmp_path) in str(info.value)


def _site_with(tmp_path, monkeypatch, repos_toml):
    text = ('host = "h"\nscheduler = "slurm"\nuser = "u"\n'
            '[paths]\nscratch = "/scratch/u"\n' + repos_toml)
    (tmp_path / "t.toml").write_text(text)
    monkeypatch.setenv("HPC_TETHER_SITES", str(tmp_path))
    return lambda: site.load("t")


def test_repos_table_loads(monkeypatch, tmp_path):
    load = _site_with(tmp_path, monkeypatch,
                      '[repos]\n'
                      'my-model = { local = "~/m/my-model", remote = "/proj/u/my-model/", branch = "main" }\n'
                      'tool-a = { local = "/l/a", remote = "/home/u/a", mode = "two-way" }\n')
    repos = load().repos
    assert repos["my-model"].remote == "/proj/u/my-model"
    assert repos["my-model"].local == str(Path("~/m/my-model").expanduser())
    assert repos["my-model"].branch == "main" and repos["my-model"].mode == "pull-only"
    assert repos["tool-a"].mode == "two-way" and repos["tool-a"].branch is None


def test_no_repos_is_empty(monkeypatch, tmp_path):
    assert _site_with(tmp_path, monkeypatch, "")().repos == {}


@pytest.mark.parametrize("entry", [
    'x = { local = "/l", remote = "/proj/u/x", brnach = "main" }',   # typo
    'x = { local = "/l", remote = "/proj/u/x", mode = "both" }',
    'x = { local = "/l", remote = "/proj" }',                         # too broad
    'x = { local = "/l", remote = "proj/u/x" }',                      # relative
    'x = { local = "/l" }',                                           # no remote
    'x = { local = "/l", remote = "/proj/u/x", branch = "-f" }',
    'x = { remote = "/proj/u/x", trusted = "yes" }',
    'x = { remote = "/proj/u/x", confirm = ["* --go*"] }',          # confirm, untrusted
    'x = { remote = "/proj/u/x", role = "my-repo" }',
    'x = { remote = "/proj/u/x", trusted = true, preview = "no" }',
    'x = { remote = "/proj/u/x", preview = false }',                 # preview, untrusted

    'x = "/proj/u/x"',
])
def test_bad_repo_entries_refused(monkeypatch, tmp_path, entry):
    load = _site_with(tmp_path, monkeypatch, "[repos]\n" + entry + "\n")
    with pytest.raises(site.SiteError):
        load()


def test_overlapping_repos_refused(monkeypatch, tmp_path):
    load = _site_with(tmp_path, monkeypatch,
                      '[repos]\na = { local = "/l", remote = "/proj/u/a" }\n'
                      'b = { local = "/m", remote = "/proj/u/a/sub" }\n')
    with pytest.raises(site.SiteError, match="overlap"):
        load()


# --- the 2026-11 layout: [policy], [repos.<name>], [filesystem], [environment]

NEW = """\
name = "t"
host = "h"
scheduler = "slurm"
user = "u"

[policy]
profile = "operate"
write_roots = ["/scratch/u", "/home/u"]
protected = ["/scratch/u/models/upstream"]
nc_max_read_mb = 50

[paths]
scratch = "/scratch/u/tmp"

[repos.my-tools]
remote = "/home/u/py/my-tools"
local = "~/code/my-tools"
trusted = true
role = "repo"
confirm = ["* --execute*"]
preview = false

[repos.trend]
remote = "/home/u/py/trend"
trusted = true

[repos.model]
remote = "/home/u/model"

[filesystem]
home = "/home/u"
aliases = { "/home/u" = "/gpfs/home/u" }

[environment]
python_bin = "/opt/py/bin"
"""


def _write(tmp_path, monkeypatch, text):
    (tmp_path / "t.toml").write_text(text)
    monkeypatch.setenv("HPC_TETHER_SITES", str(tmp_path))
    return site.load("t")


def test_new_layout_loads(monkeypatch, tmp_path):
    s = _write(tmp_path, monkeypatch, NEW)
    assert s.profile == "operate" and s.nc_max_read_mb == 50
    assert s.writable_roots == ["/scratch/u", "/home/u"]
    assert s.protected == ["/scratch/u/models/upstream"]
    assert s.paths["repo"] == "/home/u/py/my-tools"
    assert s.paths["home"] == "/home/u" and s.paths["python_bin"] == "/opt/py/bin"
    assert s.env()["HPC_PYTHON_BIN"] == "/opt/py/bin"
    assert s.aliases == {"/home/u": "/gpfs/home/u"}
    # trusted repos are the script roots; an untrusted one is not
    assert s.script_roots == ["/home/u/py/my-tools", "/home/u/py/trend"]
    assert s.script_confirm == []
    assert s.root_confirm == {"/home/u/py/my-tools": ("* --execute*",)}
    assert s.repos["trend"].local is None
    # preview defaults on; a repo turns it off for its confirm matches
    assert s.repos["my-tools"].preview is False and s.repos["trend"].preview is True
    assert s.warnings == ()


def test_old_layout_still_loads_with_notes(monkeypatch, tmp_path):
    old = ('host = "h"\nscheduler = "slurm"\nuser = "u"\nprofile = "prepare"\n'
           'writable_roots = ["/home/u"]\nscript_roots = ["/home/u/py/x"]\n'
           'script_confirm = ["* --go*"]\n'
           '[paths]\nscratch = "/scratch/u"\nhome = "/home/u"\npython_bin = "/opt/py/bin"\n'
           '[aliases]\n"/home/u" = "/gpfs/home/u"\n')
    s = _write(tmp_path, monkeypatch, old)
    assert s.profile == "prepare" and s.writable_roots == ["/home/u"]
    assert s.script_roots == ["/home/u/py/x"] and s.script_confirm == ["* --go*"]
    assert s.paths["home"] == "/home/u" and s.paths["python_bin"] == "/opt/py/bin"
    joined = " | ".join(s.warnings)
    for moved in ("[policy] profile", "[policy] write_roots", "[filesystem] home",
                  "[environment] python_bin", "[filesystem] aliases", "script_roots",
                  "script_confirm"):
        assert moved in joined


@pytest.mark.parametrize("extra", [
    'profile = "observe"\n',                       # also in [policy]
    'writable_roots = ["/home/u"]\n',
])
def test_old_and_new_place_together_refused(monkeypatch, tmp_path, extra):
    head, rest = NEW.split("[policy]", 1)
    with pytest.raises(site.SiteError, match="both set"):
        _write(tmp_path, monkeypatch, head + extra + "[policy]" + rest)


@pytest.mark.parametrize("table", ["[policy]\nprofle = \"observe\"\n",
                                   "[filesystem]\nhom = \"/home/u\"\n",
                                   "[environment]\nLD_PRELOAD = \"/x/y.so\"\n"])
def test_unknown_keys_in_new_tables_refused(monkeypatch, tmp_path, table):
    with pytest.raises(site.SiteError, match="unknown key"):
        _write(tmp_path, monkeypatch, 'host = "h"\nscheduler = "slurm"\nuser = "u"\n' + table)


def test_repo_role_clashing_with_a_path_refused(monkeypatch, tmp_path):
    text = NEW.replace('scratch = "/scratch/u/tmp"', 'scratch = "/scratch/u/tmp"\nrepo = "/x/y"')
    with pytest.raises(site.SiteError, match="already a path role"):
        _write(tmp_path, monkeypatch, text)
