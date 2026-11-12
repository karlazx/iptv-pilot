"""Plan/apply a service-scoped upgrade; preserve local images and configuration."""

import argparse
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def command(args):
    return subprocess.check_output(args, text=True).strip()


def api(url, token, payload=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        return json.load(response)


def service_images(values):
    result = {}
    for value in values:
        service, image = value.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9-]*", service):
            raise ValueError("invalid service name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]*", image):
            raise ValueError("invalid image reference")
        result[service] = image
    if not result:
        raise ValueError("at least one --service NAME=IMAGE is required")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument("--service", action="append", default=[])
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--apply", action="store_true", help="otherwise print the plan only")
    parser.add_argument("--pull", choices=["always", "missing", "never"], default="missing")
    parser.add_argument("--rollback", type=Path, help="state.json from a prior deployment")
    args = parser.parse_args()
    root = args.project_dir.resolve()
    override = root / "docker-compose.deploy.json"
    files = [root / name for name in (args.compose_file or ["docker-compose.yml"])]
    previous = json.loads(override.read_text()) if override.exists() else {"services": {}}
    desired = json.loads(json.dumps(previous))
    targets = (json.loads(args.rollback.read_text())["rollback_images"] if args.rollback
               else service_images(args.service))
    compose = ["docker", "compose", "--project-directory", str(root)]
    for path in files:
        compose += ["-f", str(path)]
    if override.exists():
        compose += ["-f", str(override)]
    # Validate without printing environment values or credentials.
    configured = json.loads(command(compose + ["--profile", "*", "config", "--format", "json"]))["services"]
    if not set(targets) <= set(configured):
        raise ValueError("unknown service in deployment plan")
    print(json.dumps({"services": targets, "apply": args.apply}, indent=2))
    if not args.apply:
        return
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    backup = root / ".deploy-history" / stamp
    backup.mkdir(parents=True, mode=0o700)
    for path in files + [root / ".env", override]:
        if path.exists():
            shutil.copy2(path, backup / path.name)
    rollback_images = {}
    for service in targets:
        identifier = command(compose + ["ps", "-q", service])
        if identifier:
            old = json.loads(command(["docker", "inspect", identifier]))[0]["Image"]
            tag = f"iptv-pilot-rollback:{service}-{stamp}"
            subprocess.run(["docker", "tag", old, tag], check=True)
            rollback_images[service] = tag
    state = {"rollback_images": rollback_images, "previous_override": previous,
             "requested": targets, "created_at": stamp}
    (backup / "state.json").write_text(json.dumps(state, indent=2))
    if not args.rollback:
        for image in sorted(set(targets.values())):
            pull = args.pull == "always"
            if args.pull == "missing":
                try:
                    command(["docker", "image", "inspect", image])
                except subprocess.CalledProcessError:
                    pull = True
            if pull:
                subprocess.run(["docker", "pull", image], check=True)
    token = os.getenv("IPTV_DEPLOY_API_TOKEN", "")
    endpoint = args.api.rstrip("/") + "/api/system/maintenance"
    before = api(endpoint, token)
    # A recorder deployment is postponed if an imminent schedule is present;
    # drain mode protects currently running tasks but cannot pause live time.
    if "recorder" in targets:
        status = api(args.api.rstrip("/") + "/api/system/status", token)
        if status.get("recordings_queued", 0) or status.get("recordings_imminent", 0):
            raise RuntimeError("recordings are due soon; choose a quieter upgrade window")
    replacement_started = False
    try:
        api(endpoint, token, {"services": list(targets), "draining": True})
        deadline = time.monotonic() + args.timeout
        quiet = 0
        while True:
            current = api(endpoint, token)
            if not any(current.get(name, {}).get("active", 0) for name in targets):
                quiet += 1
                if quiet >= 2:
                    break
            else:
                quiet = 0
            if time.monotonic() > deadline:
                raise TimeoutError("active tasks did not finish; no service was restarted")
            time.sleep(5)
        for name, image in targets.items():
            desired["services"].setdefault(name, {})["image"] = image
        pending = backup / "pending.json"
        pending.write_text(json.dumps(desired, indent=2))
        validation = ["docker", "compose", "--project-directory", str(root)]
        for path in files + [pending]:
            validation += ["-f", str(path)]
        subprocess.run(validation + ["config", "--quiet"], check=True)
        shutil.copy2(pending, override)
        applied = compose if "-f" in compose and str(override) in compose else compose + ["-f", str(override)]
        subprocess.run(applied + ["config", "--quiet"], check=True)
        replacement_started = True
        subprocess.run(applied + ["up", "-d", "--no-deps", "--pull", "never", *targets], check=True)
        deadline = time.monotonic() + 120
        while True:
            try:
                api(args.api.rstrip("/") + "/health", token)
                for service, image in targets.items():
                    cid = command(applied + ["ps", "-q", service])
                    actual = json.loads(command(["docker", "inspect", cid]))[0]
                    expected = json.loads(command(["docker", "image", "inspect", image]))[0]["Id"]
                    if actual["Image"] != expected or not actual["State"]["Running"]:
                        raise RuntimeError("service did not start on the requested image")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(3)
        print(f"Deployment verified. Rollback state: {backup / 'state.json'}")
    except Exception:
        if replacement_started and rollback_images:
            restore = json.loads(json.dumps(previous))
            for name, image in rollback_images.items():
                restore["services"].setdefault(name, {})["image"] = image
            override.write_text(json.dumps(restore, indent=2))
            restore_command = compose if str(override) in compose else compose + ["-f", str(override)]
            subprocess.run(restore_command + ["up", "-d", "--no-deps", "--pull", "never",
                                             *rollback_images], check=True)
        raise
    finally:
        # Preserve any drain that was already requested by an operator.
        for value in (False, True):
            names = [n for n in targets if before.get(n, {}).get("draining", False) == value]
            if names:
                try:
                    api(endpoint, token, {"services": names, "draining": value})
                except Exception:
                    print("Unable to restore maintenance flags; use the saved rollback state.")


if __name__ == "__main__":
    main()
