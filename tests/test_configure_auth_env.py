"""configure.sh writes auth.env with a note that ADMIN_* only seed the first login."""
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "configure.sh"


def _run(tmp_path):
    subprocess.run(["bash", str(SCRIPT)], cwd=tmp_path, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)
    return (tmp_path / "auth.env").read_text()


def test_auth_env_explains_admin_keys_are_a_one_time_seed(tmp_path):
    text = _run(tmp_path)
    note, _, rest = text.partition("ADMIN_USERNAME=")
    assert "Used once" in note and "no longer work" in note
    assert "ADMIN_PASSWORD=" in rest


def test_existing_auth_env_is_left_untouched(tmp_path):
    (tmp_path / "auth.env").write_text("ADMIN_PASSWORD=keepme\n")
    assert _run(tmp_path) == "ADMIN_PASSWORD=keepme\n"
