#!/usr/bin/env python3
"""Container Explorer integration test script for multiple disks.

Usage:
    sudo python3 tools/ce_test.py --config tools/config.yaml

Mounts disk images defined in the YAML config, runs container-explorer
commands against each, and validates results against per-disk expectations.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any

import yaml


log = logging.getLogger("ce_test")

GO_CMD = "go run cmd/main.go"
# Project root is one level up from tools/; override with CE_GO_RUN_DIR
# to test a different container-explorer checkout.
GO_RUN_DIR = os.environ.get("CE_GO_RUN_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(
    cmd: list[str],
    check: bool = True,
    timeout: int = 180,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    log.info("  Running: %s", ' '.join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd or GO_RUN_DIR,
    )
    if check and result.returncode != 0:
        log.error("  STDOUT: %s", result.stdout)
        log.error("  STDERR: %s", result.stderr)
        raise RuntimeError(f"Command failed (rc={result.returncode}): {' '.join(cmd)}")
    return result


def is_mounted(path: str) -> bool:
    result = run(["findmnt", "-n", path], check=False)
    return result.returncode == 0


def mount_disk(disk: str, mountpoint: str) -> None:
    os.makedirs(mountpoint, exist_ok=True)
    run(["mount", "-o", "loop,ro", disk, mountpoint])


def ce_command(
    mount_point: str,
    subcommand: list[str],
    output_file: str | None = None,
) -> list[str]:
    cmd = GO_CMD.split() + [
        "-i", mount_point,
    ]
    export_or_mount = subcommand and subcommand[0] in ("export", "mount")
    if not export_or_mount:
        cmd += ["--output", "json"]
        if output_file:
            cmd += ["--output-file", output_file]
    cmd += subcommand
    return cmd


def run_ce(
    mount_point: str,
    subcommand: list[str],
    output_dir: str | None = None,
    output_name: str | None = None,
) -> Any:
    outfile = os.path.join(output_dir, output_name) if output_name and output_dir else None
    cmd = ce_command(mount_point, subcommand, output_file=outfile)
    run(cmd)
    if outfile:
        with open(outfile) as f:
            return json.load(f)
    return None


def load_yaml(path: str) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)


def verify_disk(
    disk_cfg: dict[str, Any],
    output_dir: str,
    with_mounts: bool = False,
    with_exports: bool = False,
) -> None:
    mp = disk_cfg["mount_point"]

    def check(module: str, fn: Any) -> None:
        try:
            fn()
            log.info("  [PASS] %s", module)
        except Exception as e:
            log.error("  [FAIL] %s - %s", module, e)
            raise

    if "containers" in disk_cfg:
        log.info("  >> list containers")

        def _check_containers() -> None:
            data = run_ce(mp, ["list", "containers"], output_dir, "containers.json")
            _verify_list_containers(data, disk_cfg["containers"])

        check("list containers", _check_containers)

    if "drifts" in disk_cfg:
        log.info("  >> drift")

        def _check_drift() -> None:
            data = run_ce(mp, ["drift"], output_dir, "drift.json")
            _verify_drift(data, disk_cfg["drifts"])

        check("drift", _check_drift)

    mnt_base = disk_cfg.get("mnt_base", "/tmp/mnt")
    run_mounts = (
        "mounts" in disk_cfg
        and (not disk_cfg.get("skip_mounts", False) or with_mounts)
    )
    if run_mounts:
        log.info("  >> mount --all %s", mnt_base)

        def _check_mounts() -> None:
            run_ce(mp, ["mount", "--all", mnt_base])
            _verify_mounts(disk_cfg["mounts"])

        check("mount", _check_mounts)
    elif "mounts" in disk_cfg:
        log.info("  [SKIP] mount (skip_mounts: true)")

    export_base = disk_cfg.get("export_base", "/tmp/export")
    run_exports = (
        "exports" in disk_cfg
        and (not disk_cfg.get("skip_exports", False) or with_exports)
    )
    if run_exports:
        log.info("  >> export --all --image %s", export_base)
        log.info("  >> export --all --archive %s", export_base)

        def _check_exports() -> None:
            run_ce(mp, ["export", "--all", "--image", export_base])
            run_ce(mp, ["export", "--all", "--archive", export_base])
            _verify_exports(disk_cfg["exports"])

        check("export", _check_exports)
    elif "exports" in disk_cfg:
        log.info("  [SKIP] export (skip_exports: true)")


def _verify_list_containers(
    data: Any,
    expected: list[dict[str, Any]],
) -> None:
    log.info("    Verifying list containers...")
    containers = {c["ID"]: c for c in data}
    for exp in expected:
        cid = exp["id"]
        assert cid in containers, f"Container {cid} not found in output"
        c = containers[cid]
        for key, val in exp.get("checks", {}).items():
            actual = c.get(key)
            assert actual == val, (
                f"Container {cid}: expected {key}={val!r}, got {actual!r}"
            )
    log.info("    OK")


def _verify_drift(
    data: Any,
    expected: list[dict[str, Any]],
) -> None:
    log.info("    Verifying drift...")
    by_id = {d["ContainerID"]: d for d in data}
    for exp in expected:
        cid = exp["container_id"]
        assert cid in by_id, f"Drift for container {cid} not found"
        drift = by_id[cid]

        am = drift.get("AddedOrModified") or []
        iacc = drift.get("InaccessibleFiles") or []

        if "added_or_modified_count" in exp:
            assert len(am) == exp["added_or_modified_count"], (
                f"Drift {cid}: expected {exp['added_or_modified_count']} "
                f"added/modified files, got {len(am)}"
            )
        if "inaccessible_count" in exp:
            assert len(iacc) == exp["inaccessible_count"], (
                f"Drift {cid}: expected {exp['inaccessible_count']} "
                f"inaccessible files, got {len(iacc)}"
            )

        am_paths = {f["full_path"] for f in am}
        iacc_paths = {f["full_path"] for f in iacc}

        for fp in exp.get("added_or_modified", []):
            assert fp in am_paths, (
                f"Drift {cid}: expected added/modified file {fp!r} not found"
            )
        for fp in exp.get("inaccessible", []):
            assert fp in iacc_paths, (
                f"Drift {cid}: expected inaccessible file {fp!r} not found"
            )
    log.info("    OK")


def _verify_mounts(expected: list[dict[str, Any]]) -> None:
    log.info("    Verifying mounts...")
    for exp in expected:
        path = exp.get("path")
        assert os.path.isdir(path), f"Mount path {path} does not exist"
        if "contains" in exp:
            for item in exp["contains"]:
                item_path = os.path.join(path, item)
                assert os.path.exists(item_path), (
                    f"Expected {item_path} to exist under mount {path}"
                )
    log.info("    OK")


def _verify_exports(expected: list[dict[str, Any]]) -> None:
    log.info("    Verifying exports...")
    for exp in expected:
        path = exp.get("path")
        assert os.path.exists(path), f"Export path {path} does not exist"
        fmt = exp.get("type", "")
        if fmt == "archive":
            assert path.endswith(".tar.gz"), f"Expected .tar.gz archive at {path}"
        elif fmt == "image":
            assert path.endswith(".raw"), f"Expected .raw image at {path}"

        size = os.path.getsize(path)
        size_min = exp.get("size_min")
        size_max = exp.get("size_max")
        if size_min is not None:
            assert size >= size_min, (
                f"Export {path}: size {size} bytes < minimum {size_min} bytes"
            )
        if size_max is not None:
            assert size <= size_max, (
                f"Export {path}: size {size} bytes > maximum {size_max} bytes"
            )
    log.info("    OK")


def cleanup_mount(mount_point: str) -> None:
    run(["umount", "-l", mount_point], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Container Explorer integration test (multi-disk)"
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to the YAML configuration file defining disks and expectations"
    )
    parser.add_argument("--keep", action="store_true", help="Keep temp files on exit")
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    parser.add_argument("--force", action="store_true", help="Force remount if disk already mounted")
    parser.add_argument("--log", default="ce_test.log", help="Path to the log file (default: ce_test.log)")
    parser.add_argument("--with-mounts", action="store_true", help="Run mount verification even if config sets skip_mounts")
    parser.add_argument("--with-exports", action="store_true", help="Run export verification even if config sets skip_exports")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.FileHandler(args.log),
            logging.StreamHandler(),
        ],
    )
    log.info("Logging to %s", args.log)

    if os.geteuid() != 0:
        log.error("This script requires root (for mount). Re-run with sudo.")
        sys.exit(1)

    spec = load_yaml(args.config)
    disks = spec.get("disks", [])
    if not disks:
        log.error("No disks defined in YAML under 'disks' key.")
        sys.exit(1)

    global GO_CMD
    if args.debug:
        GO_CMD += " --debug"

    output_dirs: list[str] = []
    mounted: list[str] = []

    try:
        for idx, disk_cfg in enumerate(disks):
            disk_path = disk_cfg["path"]
            mount_point = disk_cfg.get("mount_point", "/mnt")
            label = f"[{os.path.basename(disk_path)} @ {mount_point}]"
            log.info("")
            log.info("=" * 60)
            log.info("Disk %d: %s", idx + 1, label)
            log.info("=" * 60)

            odir = tempfile.mkdtemp(prefix=f"ce_d{idx+1}_")
            output_dirs.append(odir)

            mnt_base = disk_cfg.get("mnt_base", "/tmp/mnt")
            export_base = disk_cfg.get("export_base", f"/tmp/export/d{idx+1}")
            os.makedirs(mnt_base, exist_ok=True)
            os.makedirs(export_base, exist_ok=True)

            log.info("  Preparing %s -> %s", disk_path, mount_point)
            already = is_mounted(mount_point)
            if already:
                if args.force:
                    log.info("  Already mounted, force-unmounting")
                    run(["umount", "-l", mount_point], check=False)
                    mount_disk(disk_path, mount_point)
                else:
                    log.info("  Already mounted, reusing")
            else:
                mount_disk(disk_path, mount_point)
            mounted.append(mount_point)

            verify_disk(disk_cfg, odir, args.with_mounts, args.with_exports)

            log.info("  [PASS] Disk %d: %s", idx + 1, label)
            log.info("  Unmounting %s", mount_point)
            cleanup_mount(mount_point)
            mounted.remove(mount_point)

        log.info("")
        log.info("=" * 60)
        log.info("All disks passed!")
        log.info("=" * 60)

    except Exception as e:
        log.error("\nFAILED: %s", e)
        log.error("  [FAIL] Disk %d", idx + 1)
        sys.exit(1)
    finally:
        if not args.keep:
            for mp in reversed(mounted):
                cleanup_mount(mp)
            for d in output_dirs:
                run(["rm", "-rf", d], check=False)
        elif mounted:
            log.error("Mounted at: %s", ", ".join(mounted))


if __name__ == "__main__":
    main()
