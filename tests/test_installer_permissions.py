"""The installer must compile sources without needing source-directory writes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def test_installer_compiles_read_only_sources_with_temporary_cache():
    script = (Path(__file__).parents[1] / 'scripts/install-core-mutators.sh').read_text()
    assert 'python -X pycache_prefix=/tmp/mcp-install-pycache -m compileall' in script
    with tempfile.TemporaryDirectory(prefix='mcp-compile-permissions-') as root:
        base = Path(root)
        base.chmod(0o755)
        source = base / 'source'
        source.mkdir()
        (source / 'sample.py').write_text('VALUE = 1\n')
        source.chmod(0o555)
        cache = base / 'cache'
        cache.mkdir()
        options = {"env": {k: v for k, v in os.environ.items() if k not in {"PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE"}}}
        if os.geteuid() == 0:
            os.chown(cache, 65534, 65534)
            options.update({'user': 65534, 'group': 65534, 'extra_groups': []})
        common = [sys.executable, '-m', 'compileall', '-q', str(source)]
        after = subprocess.run([sys.executable, '-X', f'pycache_prefix={cache}', *common[1:]], capture_output=True, text=True, **options)
        assert after.returncode == 0, after.stdout + after.stderr
        assert not (source / '__pycache__').exists()
        assert list(cache.rglob('sample.*.pyc'))
        source.chmod(0o755)
