import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROFILES = ("rsa-pss-sha256", "ecdsa-p256-sha256", "ed25519")
DEFAULT_IMAGE = Path(__file__).with_name("image.txt").read_text().strip()
EXPECTED_RESULTS = {
    "C01": (0, "OK"),
    "C02": (1, "ARTIFACT_MISMATCH"),
    "C03": (1, "KEY_NOT_TRUSTED"),
    "C04": (2, "INPUT_ERROR"),
    "C05": (0, "OK"),
}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def run_command(command, folder, label):
    """Run a command and save its output and real exit code as evidence."""
    result = subprocess.run(command, capture_output=True, timeout=300)
    write_json(folder / f"{label}.command.json", command)
    (folder / f"{label}.stdout.log").write_bytes(result.stdout)
    (folder / f"{label}.stderr.log").write_bytes(result.stderr)
    (folder / f"{label}.exit-code.txt").write_text(f"{result.returncode}\n")
    return result


def run_container(config, folder, label, mounts, command):
    docker = [
        "docker", "run", "--rm", "--pull=never", "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}",
    ]
    for mount in mounts:
        docker += ["--volume", mount]
    return run_command(docker + [config.image] + command, folder, label)


def sign_release(config, folder):
    mounts = [
        f"{folder / 'input'}:/input:ro",
        f"{folder / 'release'}:/output",
        f"{config.key}:/run/signing.key.pem:ro",
    ]
    commands = [
        "sign", "--file", f"/input/{config.artifact.name}",
        "--package", "ci-demo", "--version", "1.0.0",
        "--key", "/run/signing.key.pem", "--out", "/output/bundle.json",
    ]
    result = run_container(config, folder, "sign", mounts=mounts, command=commands)
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"{folder.name}: signing failed (exit {result.returncode}): {error}")


def verify_release(config, folder):
    mounts = [
        f"{folder / 'input'}:/input:ro",
        f"{folder / 'release'}:/output:ro",
        f"{folder / 'trust.json'}:/trust.json:ro",
    ]
    command = [
        "verify", "--file", f"/input/{config.artifact.name}", "--bundle", "/output/bundle.json",
        "--trust-store", "/trust.json", "--package", "ci-demo",
        "--version", "1.0.0", "--json",
    ]
    return run_container(config, folder, "verify", mounts=mounts, command=command)


def run_scenario(config, name):
    folder = config.out / name
    (folder / "input").mkdir(parents=True)
    (folder / "release").mkdir()
    artifact = folder / "input" / config.artifact.name
    shutil.copyfile(config.artifact, artifact)
    shutil.copyfile(config.trust_store, folder / "trust.json")
    sign_release(config, folder)

    # C01 and C05 leave the signed release unchanged
    if name == "C02":
        original = artifact.read_bytes()
        artifact.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    elif name == "C03":
        write_json(folder / "trust.json", {"schema": "minisign-trust/v1", "keys": []})
    elif name == "C04":
        artifact.unlink()

    verified = verify_release(config, folder)
    (folder / "verification.json").write_bytes(verified.stdout)
    result = json.loads(verified.stdout)

    marker = folder / "deployed.marker"
    if verified.returncode == 0 and result["valid"] is True and result["status"] == "OK":
        # Simulate deployment
        marker.write_text("Verified release is eligible for deployment.\n")

    code, status = EXPECTED_RESULTS[name]
    expected = {
        "exit_code": code, "status": status, "valid": code == 0,
        "deployment_marker": code == 0,
        "algorithm": None if name == "C04" else config.profile,
    }
    actual = {
        "exit_code": verified.returncode, "status": result["status"],
        "valid": result["valid"], "deployment_marker": marker.exists(),
        "algorithm": result["algorithm"],
    }
    passed = actual == expected
    write_json(folder / "expected.json", expected)
    write_json(folder / "actual.json", {**actual, "passed": passed})
    state = "present" if marker.exists() else "absent"
    (folder / "marker-evidence.txt").write_text(f"deployed.marker: {state}\n")
    print(f"{config.profile} {name}: {'PASS' if passed else 'FAIL'}", flush=True)
    return {"scenario": name, **actual, "passed": passed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--artifact", required=True, type=Path, help="project release archive to sign")
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--trust-store", type=Path, default=Path("ci/trust.json"))
    parser.add_argument("--out", required=True, type=Path, help="new output directory")
    config = parser.parse_args()
    config.artifact = config.artifact.resolve()
    config.key = config.key.resolve()
    config.trust_store = config.trust_store.resolve()
    config.out = config.out.resolve()
    config.out.mkdir(parents=True)  # Refuse stale output

    summary = {
        "image": config.image, "profile": config.profile,
        "artifact": config.artifact.name, "source_commit": os.environ.get("RELEASE_COMMIT"),
        "uid": os.getuid(), "gid": os.getgid(), "network": "none", "tty": False,
        "run_url": os.environ.get("CI_RUN_URL"), "results": [], "passed": False,
    }

    try:
        pulled = run_command(["docker", "pull", config.image], config.out, "pull")
        if pulled.returncode != 0:
            error = pulled.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"image pull failed (exit {pulled.returncode}): {error}")

        # All alg for C01
        scenarios = list(EXPECTED_RESULTS) if config.profile == "ed25519" else ["C01"]
        for name in scenarios:
            summary["results"].append(run_scenario(config, name))
        summary["passed"] = all(result["passed"] for result in summary["results"])
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        summary["error"] = str(error)
        print(f"ERROR: {error}", file=sys.stderr)
    finally:
        write_json(config.out / "summary.json", summary)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
