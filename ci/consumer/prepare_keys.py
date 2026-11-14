"""One-time LOCAL test setup. Never run this script in the release workflow."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess

from run_scenarios import DEFAULT_IMAGE, PROFILES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path("ci"))
    parser.add_argument("--keys-dir", type=Path, default=Path(".ci-test-keys"))
    args = parser.parse_args()
    config, private = args.config_dir.resolve(), args.keys_dir.resolve()
    if private.exists() or (config / "trust.json").exists() or (config / "keys").exists():
        parser.error("refusing to replace existing keys or trusted configuration")
    subprocess.run(["docker", "pull", DEFAULT_IMAGE], check=True)
    private.mkdir(parents=True, mode=0o700)
    # Both key generation and trust setup happen locally, before any consumer run.
    command = ["docker", "run", "--rm", "--pull=never", "--network", "none", "--user",
               f"{os.getuid()}:{os.getgid()}", "--mount", f"type=bind,src={private},dst=/setup", DEFAULT_IMAGE]
    for profile in PROFILES:
        subprocess.run([*command, "keygen", "--algorithm", profile, "--out", f"/setup/{profile}"], check=True)
        subprocess.run([*command, "trust", "add", "--key", f"/setup/{profile}.pub.pem",
                        "--package", "ci-demo", "--store", "/setup/trust.json"], check=True)
    (config / "keys").mkdir(parents=True)
    for profile in PROFILES:
        shutil.move(private / f"{profile}.pub.pem", config / "keys" / f"{profile}.pub.pem")
    shutil.move(private / "trust.json", config / "trust.json")
    print(f"Public configuration: {config / 'trust.json'} and {config / 'keys'}")
    print(f"Private TEST keys: {private}. Store each in its CI secret; never upload as evidence.")


if __name__ == "__main__":
    main()
