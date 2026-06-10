from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _fake_playwright_install(tmp_path: Path) -> Path:
    node_modules = tmp_path / "node_modules"
    package_dir = node_modules / "playwright"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text(
        '{"name":"playwright","main":"index.js"}',
        encoding="utf-8",
    )
    (package_dir / "index.js").write_text(
        'module.exports = { marker: "fake-playwright" };\n',
        encoding="utf-8",
    )
    bin_dir = node_modules / ".bin"
    bin_dir.mkdir()
    cli = bin_dir / "playwright"
    cli.write_text('#!/usr/bin/env sh\nprintf "fake-playwright-cli %s\\n" "$*"\n', encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return node_modules


def test_playwright_node_uses_configured_node_modules(tmp_path: Path) -> None:
    node_modules = _fake_playwright_install(tmp_path)
    env = {**os.environ, "WLCODEX_PLAYWRIGHT_NODE_MODULES": str(node_modules)}

    result = subprocess.run(
        [
            str(ROOT / "scripts" / "playwright-node"),
            "-e",
            'process.stdout.write(require("playwright").marker)',
        ],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "fake-playwright"


def test_playwright_cli_uses_configured_node_modules(tmp_path: Path) -> None:
    node_modules = _fake_playwright_install(tmp_path)
    env = {**os.environ, "WLCODEX_PLAYWRIGHT_NODE_MODULES": str(node_modules)}

    result = subprocess.run(
        [str(ROOT / "scripts" / "playwright"), "--version"],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "fake-playwright-cli --version\n"
