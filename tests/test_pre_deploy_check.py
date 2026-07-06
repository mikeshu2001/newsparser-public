from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path("scripts/pre_deploy_check.sh")


def test_pre_deploy_check_keeps_compose_config_validation() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "docker compose version >/dev/null" in script
    assert "docker_compose config --quiet" in script


def test_pre_deploy_check_build_requires_skip_off_and_daemon_available() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert '${SKIP_DOCKER_BUILD:-0}" = "1"' in script
    assert "docker info >/dev/null 2>&1" in script
    assert "Docker daemon is unavailable" in script
    assert "docker_compose build" in script


def test_pre_deploy_check_has_no_unused_tmp_compose_cleanup() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "/tmp/news-parser-compose-config.yml" not in script


def test_pre_deploy_check_executes_expected_skip_build_flow(tmp_path: Path) -> None:
    call_log = tmp_path / "calls.log"
    fake_python = tmp_path / "python"
    fake_docker = tmp_path / "docker"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"python $*\" >> \"$CALL_LOG\"\n",
        encoding="utf-8",
    )
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"docker $*\" >> \"$CALL_LOG\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_docker.chmod(0o755)

    env = {
        **os.environ,
        "CALL_LOG": str(call_log),
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PYTHON_BIN": str(fake_python),
        "POSTGRES_PASSWORD": "predeploy_local_placeholder",
        "SKIP_DOCKER_BUILD": "1",
    }

    result = subprocess.run(
        [str(SCRIPT)],
        cwd=Path.cwd(),
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert "Docker build skipped because SKIP_DOCKER_BUILD=1" in result.stdout
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "python -m compileall -q app scripts healthcheck.py",
        "python scripts/safety_check.py",
        "python -m pytest",
        "docker compose version",
        "docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet",
    ]
