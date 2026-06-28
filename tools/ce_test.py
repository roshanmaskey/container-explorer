#!/usr/bin/env python3
"""Container Explorer integration test script for multiple disks.

Usage:
    sudo python3 tools/ce_test.py --config tools/config.yaml

Mounts disk images defined in the YAML config, runs container-explorer
commands against each, and validates results against per-disk expectations.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile

import yaml


log = logging.getLogger("ce_test")

GO_CMD = "go run cmd/main.go"
# Project root is one level up from tools/
GO_RUN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd, check=True, timeout=180, cwd=None):
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


def is_mounted(path):
    result = run(["findmnt", "-n", path], check=False)
    return result.returncode == 0


def mount_disk(disk, mountpoint):
    os.makedirs(mountpoint, exist_ok=True)
    run(["mount", "-o", "loop,ro", disk, mountpoint])


def ce_command(mount_point, subcommand, output_file=None):
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


def run_ce(mount_point, subcommand, output_dir=None, output_name=None):
    outfile = os.path.join(output_dir, output_name) if output_name and output_dir else None
    cmd = ce_command(mount_point, subcommand, output_file=outfile)
    run(cmd)
    if outfile:
        with open(outfile) as f:
            return json.load(f)
    return None


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def verify_disk(disk_cfg, output_dir):
    mp = disk_cfg["mount_point"]

    if "containers" in disk_cfg:
        log.info("  >> list containers")
        data = run_ce(mp, ["list", "containers"], output_dir, "containers.json")
        _verify_list_containers(data, disk_cfg["containers"])

    if "drifts" in disk_cfg:
        log.info("  >> drift")
        data = run_ce(mp, ["drift"], output_dir, "drift.json")
        _verify_drift(data, disk_cfg["drifts"])

    mnt_base = disk_cfg.get("mnt_base", "/tmp/mnt")
    if "mounts" in disk_cfg:
        log.info("  >> mount --all %s", mnt_base)
        run_ce(mp, ["mount", "--all", mnt_base])
        _verify_mounts(disk_cfg["mounts"])

    export_base = disk_cfg.get("export_base", "/tmp/export")
    if "exports" in disk_cfg:
        log.info("  >> export --all --image %s", export_base)
        run_ce(mp, ["export", "--all", "--image", export_base])
        log.info("  >> export --all --archive %s", export_base)
        run_ce(mp, ["export", "--all", "--archive", export_base])
        _verify_exports(disk_cfg["exports"])


def _verify_list_containers(data, expected):
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


def _verify_drift(data, expected):
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


def _verify_mounts(expected):
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


def _verify_exports(expected):
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


def cleanup_mount(mount_point):
    run(["umount", "-l", mount_point], check=False)


def main():
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

    output_dirs = []
    mounted = []

    try:
        for idx, disk_cfg in enumerate(disks):
            disk_path = disk_cfg["path"]
            mount_point = disk_cfg.get("mount_point", f"/mnt/d{idx+1}")
            label = f"[{os.path.basename(disk_path)} @ {mount_point}]"
            log.info("")
            log.info("=" * 60)
            log.info("Disk %d: %s", idx + 1, label)
            log.info("=" * 60)

            odir = tempfile.mkdtemp(prefix=f"ce_d{idx+1}_")
            output_dirs.append(odir)

            mnt_base = disk_cfg.get("mnt_base", f"/tmp/mnt/d{idx+1}")
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

            verify_disk(disk_cfg, odir)

        log.info("")
        log.info("=" * 60)
        log.info("All disks passed!")
        log.info("=" * 60)

    except Exception as e:
        log.error("\nFAILED: %s", e)
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
