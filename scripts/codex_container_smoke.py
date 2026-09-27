"""Check exact app/bridge images with managed private auth, never a live account."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
from uuid import uuid4

from container_smoke import INIT_CHECK, docker


ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = "/run/codex-auth/token"

# These programs execute inside the candidate containers. Credentials stay there,
# including on failures; only fixed boolean proof fields may leave the container.
HTTP = """
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def request(base, path, *, token=None, data=None, method='GET', headers=None, expected=200):
    headers = dict(headers or {})
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(data).encode()
    try:
        response = opener.open(urllib.request.Request(base + path, data=data, headers=headers,
                                                      method=method), timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        assert response.status == expected
        raw = response.read(65537)
        assert len(raw) <= 65536
        return json.loads(raw) if raw else None
"""

STATUS_CHECK = HTTP + """
from backend.config import Settings

assert os.getuid() == os.getgid() == 10001
private = Path('/run/codex-auth/token').read_text()
assert Settings.from_env().codex_bridge_token == private
assert not os.environ.get('OPENAI_API_KEY') and not os.environ.get('CODEX_BRIDGE_TOKEN')
os.environ['CODEX_BRIDGE_TOKEN'] = '0' * 64
try:
    Settings.from_env()
except ValueError:
    pass
else:
    raise AssertionError('Direct and file credentials must be mutually exclusive')
os.environ.pop('CODEX_BRIDGE_TOKEN')

base = 'http://127.0.0.1:8000'
root = '/api/research/codex'
disconnected = {'enabled': True, 'state': 'disconnected', 'verification_url': None,
                'user_code': None, 'generation_enabled': False}
operators = [request(base, '/api/login', method='POST',
                     data={'password': os.environ['ADMIN_PASSWORD']})['token'] for _ in range(2)]
assert operators[0] != operators[1]
try:
    code = request(base, '/api/pairings', token=operators[0], data={}, method='POST')['code']
    wearer = request(base, '/api/pair', data={'code': code, 'name': 'Private smoke'}, method='POST')['token']
    for token in (None, 'invalid-smoke-bearer', private, wearer):
        request(base, root + '/status', token=token, expected=401)
    request(base, root + '/status', headers={'Cookie': 'evencomms_playback=' + operators[0]}, expected=401)
    # Do not put even disposable credentials in URLs or invoke the Codex login route.
    request(base, root + '/status?token=invalid-smoke-bearer', expected=401)
    for token in operators:
        assert request(base, root + '/status', token=token) == disconnected
        request(base, root + '/models', token=token, expected=409)
    assert request(base, root + '/connection', token=operators[0], method='DELETE') == disconnected
    assert request(base, root + '/status', token=operators[1]) == disconnected
    request(base, '/api/logout', token=operators[0], method='POST', expected=204)
    request(base, root + '/status', token=operators[0], expected=401)
    assert request(base, root + '/status', token=operators[1]) == disconnected
finally:
    request(base, '/api/logout', token=operators[1], method='POST', expected=204)
print(json.dumps({'operator_isolation': True, 'enabled_disconnected': True, 'file_auth': True}))
"""

READY_CHECK = HTTP + """
from backend.config import Settings

config = Settings.from_env()
private = Path('/run/codex-auth/token').read_text()
assert config.codex_bridge_token == private
base = config.codex_bridge_url
assert base == 'http://codex-bridge:8001'
assert request(base, '/health') == {'ok': True}
for token in (None, 'invalid-smoke-bearer'):
    request(base, '/ready', token=token, expected=401)
request(base, '/ready', token=private, headers={'Origin': 'https://smoke.invalid'}, expected=403)
ready = request(base, '/ready', token=private)
assert set(ready) == {'binary_verified', 'generation_enabled', 'active_sessions'}
assert ready['binary_verified'] is True and ready['generation_enabled'] is True
assert type(ready['active_sessions']) is int and ready['active_sessions'] == 0
assert request(base, '/sessions/' + '0' * 64 + '/status', token=private) == {
    'state': 'disconnected', 'verification_url': None, 'user_code': None, 'generation_enabled': False}
print(json.dumps({'binary_verified': True, 'generation_enabled': True,
                  'no_account_sessions': True, 'private_auth': True}))
"""

BRIDGE_CHECK = HTTP + """
import resource

assert os.getuid() == os.getgid() == 10002
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
assert not os.environ.get('OPENAI_API_KEY') and not os.environ.get('CODEX_BRIDGE_TOKEN')
private = Path('/run/codex-auth/token').read_text()
assert len(private) == 64
assert request('http://127.0.0.1:8001', '/ready', token=private) == {
    'binary_verified': True, 'generation_enabled': True, 'active_sessions': 0}
assert not any(Path(path).exists() for path in ('/data', '/config', '/var/run/docker.sock'))
assert not list(Path('/tmp').rglob('auth.json'))
assert not list(Path('/tmp').glob('evencomms-*'))
print(json.dumps({'bridge_file_auth': True, 'ephemeral_cleanup': True, 'no_app_data': True}))
"""

UID101_CHECK = """
import json
import os
from pathlib import Path
assert os.getuid() == os.getgid() == 101
try:
    Path('/run/codex-auth/token').read_bytes()
except PermissionError:
    pass
else:
    raise AssertionError('UID 101 must not read the private token')
print(json.dumps({'uid101_denied': True}))
"""

OFFLINE_PROBE = """
import asyncio
import json
import os
from codex_bridge.policy import generation_allowed
from codex_bridge.probe import run_probe

assert os.getuid() == os.getgid() == 10002
assert not os.environ.get('OPENAI_API_KEY')
report = asyncio.run(run_probe('/usr/local/bin/codex', temp_parent='/tmp'))
assert report['binary_verified'] is True and report['generation_enabled'] is True
assert report['synthetic_device_login'] is True and generation_allowed(report)
assert report['live_account_verified'] is False
print(json.dumps({'offline_binary': True, 'synthetic_device_flow': True,
                  'production_generation_gate': True, 'live_account_verified': False}))
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def image_info(reference, user, labels):
    info = json.loads(docker("image", "inspect", reference))[0]
    require(info["Os"] == "linux" and info["Architecture"] == "amd64", "Expected linux/amd64 candidate")
    require(info["Config"]["User"] == user, "Unexpected candidate image user")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", info["Id"]), "Expected immutable local image ID")
    actual = info["Config"].get("Labels") or {}
    require(all(actual.get(key) == value for key, value in labels.items()), "Candidate OCI labels do not match source")
    return info["Id"]


def prepare_config(config, app_id, bridge_id, project, data_root):
    """Keep source Umbrel controls, replacing only images, owned storage and ingress."""
    services = config["services"]
    required = {"data_init", "server", "codex-bridge", "web", "mediamtx"}
    require(required <= services.keys(), "Managed stack must include the installed, idle bridge")
    config["services"] = services = {name: services[name] for name in sorted(required)}
    require("codex-bridge" not in services["server"].get("depends_on", {}),
            "The backend must start independently of bridge health")
    require(services["codex-bridge"].get("depends_on", {}).get("data_init", {}).get("condition")
            == "service_completed_successfully", "Bridge must depend on the offline initializer")
    initializer = services["data_init"]
    require(initializer["command"] == ["python", "-m", "backend.init_data", "--config-dir", "/config",
                                       "--codex-auth-dir", "/codex-auth"], "Unexpected managed initializer contract")
    sources = {}
    for mount in initializer["volumes"]:
        target = mount["target"]
        require(target in {"/data", "/config", "/codex-auth"} and not mount.get("read_only"),
                "Unexpected initializer storage")
        require(mount["type"] == "bind" and Path(mount["source"]).parent == data_root,
                "Initializer storage must be dedicated to the test")
        sources[mount["source"]] = target.lstrip("/")
    require(set(sources.values()) == {"data", "config", "codex-auth"}, "Private auth must have its own directory")
    for name, service in services.items():
        require(not service.get("container_name") and not service.get("external_links"), "Stack must be project-isolated")
        # Compose's normalized JSON encodes a scalar zero ulimit as {}, which
        # cannot be loaded back as Compose input. Restore its exact zero value.
        for limit, value in service.get("ulimits", {}).items():
            if value == {}:
                service["ulimits"][limit] = 0
        for mount in service.get("volumes", []):
            require(mount["type"] == "bind" and mount["source"] in sources, "Unexpected host mount in managed stack")
            mount["type"], mount["source"] = "volume", sources[mount["source"]]
            mount.pop("bind", None)
            mount["volume"] = {"nocopy": True}
        if name in {"data_init", "server", "codex-bridge"}:
            service["image"] = bridge_id if name == "codex-bridge" else app_id
            service["pull_policy"] = "never"
            service.pop("build", None)
        else:
            require(re.search(r"@sha256:[0-9a-f]{64}$", service["image"]), "Media/proxy images must remain digest-pinned")
        if name in {"server", "codex-bridge"}:
            environment = service["environment"]
            require(environment.get("CODEX_BRIDGE_TOKEN_FILE") == TOKEN_FILE
                    and not environment.get("CODEX_BRIDGE_TOKEN") and not environment.get("OPENAI_API_KEY"),
                    "Runtime must use file-only private auth and no API key")
        if name == "server":
            # The disposable operator password remains in the subprocess environment.
            service["environment"]["ADMIN_PASSWORD"] = "${ADMIN_PASSWORD:?}"
    initializer["command"] = ["python", "-c", INIT_CHECK]
    for name, port in (("web", 8080), ("mediamtx", 1935)):
        services[name]["ports"] = [{"target": port, "published": "0", "host_ip": "127.0.0.1", "protocol": "tcp"}]
    config["name"] = project
    config["volumes"] = {name: {"name": project + "-" + name} for name in sources.values()}
    for network in config["networks"].values():
        require(not network.get("external") and network["name"].startswith(project + "_"),
                "Networks must belong to this smoke test")
    return config


def inspect_controls(info, image_id, *, offline=False):
    require(info["Image"] == image_id, "Container is not running the captured image ID")
    host = info["HostConfig"]
    require(info["Config"]["User"] == "10002:10002" and host["Init"] and host["ReadonlyRootfs"],
            "Bridge must run non-root with init and a read-only root")
    require(set(host.get("CapDrop") or []) == {"ALL"} and not host.get("CapAdd") and not host["Privileged"],
            "Bridge capabilities are not isolated")
    require(any(value in {"no-new-privileges", "no-new-privileges:true"} for value in host["SecurityOpt"]),
            "Bridge must prohibit privilege escalation")
    require(host["Memory"] == host["MemorySwap"] == 1024**3 and host["NanoCpus"] == 10**9
            and host["PidsLimit"] == 128, "Bridge CPU, memory, swap or PID limits differ from the deployment contract")
    require(any(row == {"Name": "core", "Soft": 0, "Hard": 0} for row in host["Ulimits"]), "Core dumps must be disabled")
    tmpfs = host.get("Tmpfs") or {}
    options = set(tmpfs.get("/tmp", "").split(","))
    require(set(tmpfs) == {"/tmp"} and {"rw", "noexec", "nosuid", "nodev"} <= options
            and bool({"size=256m", "size=268435456"} & options), "Bridge scratch space must be bounded, non-executable tmpfs")
    require(not host.get("PortBindings") and not host["PublishAllPorts"]
            and not any((info["NetworkSettings"]["Ports"] or {}).values()), "Bridge must never publish a port")
    require(not host.get("Binds") and not host.get("Devices") and not host.get("VolumesFrom")
            and host.get("PidMode", "") == "" and host.get("IpcMode") == "private",
            "Bridge must not share host resources")
    if offline:
        require(host["NetworkMode"] == "none" and not [row for row in info["Mounts"] if row["Type"] != "tmpfs"],
                "Offline proof must have no network or mounted credentials")


def check_mounts(info, expected, project):
    mounts = {row["Destination"]: row for row in info["Mounts"] if row["Type"] != "tmpfs"}
    require(set(mounts) == set(expected), "Unexpected runtime mounts")
    for target, (volume, writable) in expected.items():
        row = mounts[target]
        require(row["Type"] == "volume" and row["Name"] == project + "-" + volume and row["RW"] is writable,
                "Runtime storage ownership or access differs from the private contract")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Local app image ID/tag; never pulled or rebuilt")
    parser.add_argument("--codex-image", required=True, help="Local bridge image ID/tag; never pulled or rebuilt")
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source", required=True, help="Expected OCI source URL")
    parser.add_argument("--browser", action="store_true", help="Also run real RTMP/HLS/Chromium and synthetic Research UI checks")
    args = parser.parse_args()
    labels = {"org.opencontainers.image." + key: value for key, value in
              (("version", args.version), ("revision", args.revision), ("source", args.source))}
    app_id = image_info(args.image, "10001:10001", labels)
    bridge_id = image_info(args.codex_image, "10002:10002", {**labels, "org.opencontainers.image.codex-version": "0.157.1"})
    project = "evencomms-codex-smoke-" + uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    stage = "managed Compose configuration"
    with tempfile.TemporaryDirectory(prefix=project + "-") as directory:
        work = Path(directory)
        environment = {"APP_DATA_DIR": str(work), "APP_PASSWORD": password, "ADMIN_PASSWORD": password,
                       "OPENAI_API_KEY": "", "CODEX_BRIDGE_TOKEN": "", "OLLAMA_URL": "", "STT_ENABLED": "false",
                       "STREAM_ENABLED": "true", "PUBLIC_HOST": "localhost", "COOKIE_SECURE": "false",
                       "ALLOWED_ORIGINS": "", "COMPOSE_PROFILES": ""}
        compose = ("compose", "--project-name", project, "--env-file", "/dev/null", "-f", str(work / "compose.json"))

        def dc(*command, timeout=180, check=True):
            return docker(*compose, *command, env=environment, timeout=timeout, check=check)

        def container(service):
            identity = dc("ps", "--all", "--quiet", service)
            require(re.fullmatch(r"[0-9a-f]{64}", identity), "Expected exactly one owned service container")
            return json.loads(docker("inspect", identity))[0]

        def execute(service, program, *, user=None):
            command = ["exec", "-i"]
            if user:
                command.extend(("--user", user))
            command.extend((container(service)["Id"], "python", "-"))
            proof = json.loads(docker(*command, stdin=program))
            require(proof and all(value is True for value in proof.values()), "Container boolean proof failed")

        try:
            config = json.loads(docker("compose", "--project-name", project, "--env-file", "/dev/null",
                "-f", str(ROOT / "deploy/umbrel/docker-compose.yml"),
                "-f", str(ROOT / "deploy/umbrel/ci.override.yml"), "config", "--format", "json", env=environment))
            config = prepare_config(config, app_id, bridge_id, project, work)
            (work / "compose.json").write_text(json.dumps(config))
            (work / "compose.json").chmod(0o600)
            dc("config", "--quiet")

            stage = "network-none real Codex production probe"
            print("Checking pinned Codex with offline synthetic device/provider fixtures, not a live account", flush=True)
            proof = json.loads(docker("run", "--name", project + "-offline", "--pull=never", "--init",
                "--user", "10002:10002", "--read-only", "--network", "none", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777",
                "--memory", "1g", "--memory-swap", "1g", "--cpus", "1", "--pids-limit", "128", "--ulimit", "core=0:0",
                "--env", "OPENAI_API_KEY=", bridge_id, "python", "-c", OFFLINE_PROBE))
            require(proof == {"offline_binary": True, "synthetic_device_flow": True,
                              "production_generation_gate": True, "live_account_verified": False}, "Offline production gate failed")
            inspect_controls(json.loads(docker("inspect", project + "-offline"))[0], bridge_id, offline=True)

            stage = "offline managed init and private token idempotency"
            dc("create", "--no-build", "data_init")
            init = container("data_init")
            require(init["Image"] == app_id and init["Config"]["User"] == "0:0"
                    and init["HostConfig"]["NetworkMode"] == "none" and init["HostConfig"]["ReadonlyRootfs"],
                    "Initializer must use the exact candidate, offline as read-only root")
            check_mounts(init, {"/data": ("data", True), "/config": ("config", True),
                                "/codex-auth": ("codex-auth", True)}, project)
            initialized = json.loads(docker("start", "--attach", init["Id"]))
            require(initialized == {"private_init": True, "token_preserved": True}
                    and container("data_init")["State"]["ExitCode"] == 0, "Managed initialization failed")

            stage = "backend independence and operator isolation without a bridge"
            dc("up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "90", "server")
            server = container("server")
            require(server["Image"] == app_id and not server["HostConfig"].get("PortBindings"), "Backend image or private ingress mismatch")
            check_mounts(server, {"/data": ("data", True), "/run/codex-auth": ("codex-auth", False)}, project)
            execute("server", STATUS_CHECK)
            execute("server", UID101_CHECK, user="101:101")

            stage = "actual bridge startup gate, private authentication and runtime isolation"
            dc("up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "150", "codex-bridge")
            bridge = container("codex-bridge")
            inspect_controls(bridge, bridge_id)
            check_mounts(bridge, {"/run/codex-auth": ("codex-auth", False)}, project)
            for service, expected in ((server, {"private", "codex_link"}), (bridge, {"codex_link", "codex_egress"})):
                require(set(service["NetworkSettings"]["Networks"]) == {config["networks"][key]["name"] for key in expected},
                        "Backend/bridge network membership differs from the private contract")
            for key, internal in (("codex_link", True), ("codex_egress", False)):
                network = json.loads(docker("network", "inspect", config["networks"][key]["name"]))[0]
                require(network["Internal"] is internal and network["Driver"] == "bridge", "Private link/egress isolation mismatch")
            execute("server", READY_CHECK)
            execute("server", STATUS_CHECK)
            execute("codex-bridge", BRIDGE_CHECK)
            execute("codex-bridge", UID101_CHECK, user="101:101")

            stage = "media/proxy private-secret exclusion"
            dc("pull", "web", "mediamtx", timeout=300)
            dc("up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "90", "web", "mediamtx")
            for name in ("web", "mediamtx"):
                info = container(name)
                check_mounts(info, {"/config": ("config", False)}, project)
                require(not any(value.startswith("CODEX_BRIDGE_") for value in info["Config"]["Env"]),
                        "Media/proxy must not receive private bridge configuration")

            if args.browser:
                stage = "exact-candidate RTMP/HLS/browser and synthetic Research UI"
                port = container("web")["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
                rtmp = container("mediamtx")["NetworkSettings"]["Ports"]["1935/tcp"][0]["HostPort"]
                stream = subprocess.Popen([sys.executable, str(ROOT / "scripts/stream_smoke.py"),
                    "--base-url", "http://127.0.0.1:" + port, "--rtmp-host", "127.0.0.1", "--rtmp-port", rtmp,
                    "--browser", "--research"], env={**os.environ, **environment}, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    stream.communicate(timeout=360)
                    require(stream.returncode == 0, "Candidate stream/browser smoke failed; private diagnostics suppressed")
                finally:
                    if stream.poll() is None:
                        # Give stream_smoke's signal/finally handlers time to reap
                        # its publisher and browser before removing the stack.
                        stream.terminate()
                        try:
                            stream.communicate(timeout=45)
                        except subprocess.TimeoutExpired:
                            stream.kill()
                            stream.communicate(timeout=5)
                print("Exact candidate RTMP, authenticated HLS, Chromium playback and synthetic Research UI passed", flush=True)
            execute("server", READY_CHECK)
            print("Candidate pair passed: managed auth, startup gate, idle account, operator isolation and runtime controls", flush=True)
        except Exception:
            raise RuntimeError(f"Codex container smoke failed during {stage}; private diagnostics suppressed") from None
        finally:
            # No global prune, raw container logs, or resources belonging to another stack.
            for cleanup in (("rm", "--force", project + "-offline"),
                            (*compose, "down", "--volumes", "--remove-orphans", "--timeout", "15")):
                try:
                    docker(*cleanup, env=environment, check=False)
                except RuntimeError:
                    print("Warning: owned Codex smoke resource cleanup failed", file=sys.stderr)


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise RuntimeError("Container smoke interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        main()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("Codex container smoke failed; private diagnostics suppressed", file=sys.stderr)
        sys.exit(1)
