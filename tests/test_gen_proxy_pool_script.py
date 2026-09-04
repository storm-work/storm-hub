"""scripts/gen_proxy_pool.py -- Phase 3 (D32) relocated its credential construction into
libs/proxies/credentials.py, leaving the script as a thin CLI wrapper (country-splitting +
argparse + file writing). Proves the CLI still runs end-to-end as a real subprocess (how it's
actually used operationally) and that its output is unchanged by the relocation.

SAFETY: the script's default --out path is data/proxies_{provider}.txt -- real, currently-used
pool files (~1.3MB/~440KB, confirmed present on disk). Every invocation here passes --out pointed
at tmp_path, so this suite can never overwrite live pool data (same discipline as
tests/test_cleanup_checkpoints_script.py after the Phase 1.1 incident)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "gen_proxy_pool.py"


def _run(*args, env=None):
    """`env`, when given, is the COMPLETE subprocess environment -- not merged on top of this
    process's own os.environ. A merge would silently re-leak a var a caller tried to exclude:
    apps.hub.config calls load_dotenv() as an import side effect, so once any other test module in
    the same pytest session has imported it, THIS process's os.environ already carries
    SOAX_PASSWORD/WEBSHARE_PASSWORD from .env regardless of what the test intended to strip
    (caught by running this file combined with tests/hub/, not in isolation)."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, cwd=str(_ROOT),
        env=env if env is not None else os.environ,
    )


def test_soax_generates_expected_line_count_and_grammar(tmp_path):
    out = tmp_path / "soax_out.txt"
    r = _run("soax", "--count", "10", "--countries", "DE,IT", "--password", "testpw", "--out", str(out))
    assert r.returncode == 0, r.stderr
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 10
    assert all(line.startswith("package-352438-country-") for line in lines)
    assert all("-opt-wb:testpw@proxy.soax.com:5000" in line for line in lines)


def test_webshare_generates_expected_line_count_and_grammar(tmp_path):
    out = tmp_path / "webshare_out.txt"
    r = _run("webshare", "--count", "10", "--countries", "DE,IT", "--password", "testpw", "--out", str(out))
    assert r.returncode == 0, r.stderr
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 10
    assert all(line.startswith("p.webshare.io:80:sodade2026-") for line in lines)
    assert all(line.endswith(":testpw") for line in lines)


def test_soax_no_geo_omits_country_segment(tmp_path):
    out = tmp_path / "soax_nogeo.txt"
    r = _run("soax", "--count", "5", "--countries", "DE", "--password", "testpw", "--no-geo",
            "--out", str(out))
    assert r.returncode == 0, r.stderr
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 5
    assert all("-country-" not in line for line in lines)
    assert "no country pinned" in r.stdout


def test_generated_lines_parse_via_the_real_importer(tmp_path):
    """Functional parity, not just format-string parity -- every line the CLI writes must still
    import cleanly through the dashboard's real Import popup path."""
    from apps.hub.importers import parse_proxies

    out = tmp_path / "soax_parity.txt"
    r = _run("soax", "--count", "20", "--countries", "PT,DE,IT", "--password", "testpw",
            "--out", str(out))
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    parsed, errors = parse_proxies(text)
    assert errors == []
    assert len(parsed) == 20


def test_missing_password_fails_loudly_not_silently(tmp_path):
    out = tmp_path / "should_not_exist.txt"
    env = {k: v for k, v in os.environ.items() if k not in ("SOAX_PASSWORD", "WEBSHARE_PASSWORD")}
    r = _run("soax", "--count", "5", "--out", str(out), env=env)
    assert r.returncode != 0
    assert "password" in r.stderr.lower()
    assert not out.exists()


def test_session_ids_are_unique_across_a_generated_batch(tmp_path):
    import re

    out = tmp_path / "soax_unique.txt"
    r = _run("soax", "--count", "300", "--countries", "DE", "--password", "testpw", "--out", str(out))
    assert r.returncode == 0, r.stderr
    ids = [m.group(1) for m in re.finditer(r"sessionid-(\w{16})-sessionlength", out.read_text())]
    assert len(ids) == 300
    assert len(set(ids)) == 300


def test_counts_split_as_evenly_as_possible_across_countries(tmp_path):
    out = tmp_path / "soax_split.txt"
    r = _run("soax", "--count", "10", "--countries", "DE,IT,PT", "--password", "testpw", "--out", str(out))
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    counts = {cc: text.count(f"-country-{cc.lower()}-") for cc in ("de", "it", "pt")}
    assert sorted(counts.values()) == [3, 3, 4]   # 10 over 3 -> remainder to the first countries


def test_help_still_documents_usage(tmp_path):
    r = _run("--help")
    assert r.returncode == 0
    assert "soax" in r.stdout and "webshare" in r.stdout


@pytest.fixture(scope="module", autouse=True)
def _real_pool_files_are_never_touched():
    """Belt-and-suspenders around every test above: snapshots the real data/proxies_{provider}.txt
    files' mtime+size before this module's tests run and re-checks after, so an --out regression
    that fell back to the script's default path would fail this instead of silently clobbering
    live pool data."""
    paths = [_ROOT / "data" / "proxies_soax.txt", _ROOT / "data" / "proxies_webshare.txt"]
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.exists()}
    yield
    for p, snap in before.items():
        assert (p.stat().st_mtime_ns, p.stat().st_size) == snap, f"{p} was modified by this test module"
