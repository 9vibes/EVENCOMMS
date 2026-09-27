"""Daemon-free regression checks for release helpers; no registry/account requests."""

import copy
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import urllib.error
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[2]
APP_ID = "sha256:" + "a" * 64
BRIDGE_ID = "sha256:" + "b" * 64


@pytest.fixture
def checks(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import codex_container_smoke
    return codex_container_smoke


def release_python(index):
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    programs = re.findall(r"^          python - <<'PY'\n(.*?)^          PY$", workflow, re.M | re.S)
    assert len(programs) == 3
    return compile(textwrap.dedent(programs[index]), "release-inline-python", "exec")


@pytest.mark.parametrize("requested,ref_type,ref_name,valid", [
    ("", "branch", "main", True), ("0.4.1", "tag", "v0.4.1", True),
    ("0.4.1", "tag", "v0.4.0", False), ("0.4.0", "branch", "main", False),
    ("v0.4.1", "branch", "main", False), ("0.4.1+build", "branch", "main", False),
    ("0.4.1-01", "branch", "main", False), ("0.4.1-" + "a" * 117, "branch", "main", False),
])
def test_release_version_gate(monkeypatch, tmp_path, requested, ref_type, ref_name, valid):
    monkeypatch.chdir(ROOT)
    for key, value in {"REQUESTED_VERSION": requested, "GITHUB_REF_TYPE": ref_type,
                       "GITHUB_REF_NAME": ref_name, "GITHUB_OUTPUT": str(tmp_path / "output")}.items():
        monkeypatch.setenv(key, value)
    if valid:
        exec(release_python(0), {})
        assert (tmp_path / "output").read_text() == "version=0.4.1\n"
    else:
        with pytest.raises(SystemExit) as error:
            exec(release_python(0), {})
        if len(requested + "-codex") > 128:
            assert "room for -codex" in str(error.value)
        assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("directory", ["frontend", "codex_bridge"])
def test_release_rejects_mismatched_package_lock_root(monkeypatch, directory):
    original = Path.read_text

    def read(path, *args, **kwargs):
        content = original(path, *args, **kwargs)
        if path == Path(directory, "package-lock.json"):
            lock = json.loads(content)
            lock["packages"][""]["version"] = "0.3.0"
            return json.dumps(lock)
        return content

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("REQUESTED_VERSION", "0.4.1")
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(SystemExit, match="package-lock root versions"):
        exec(release_python(0), {})


@pytest.mark.parametrize("responses,valid", [
    ([(404, "MANIFEST_UNKNOWN"), (404, "MANIFEST_UNKNOWN")], True),
    ([(404, "NAME_UNKNOWN"), (404, "MANIFEST_UNKNOWN")], True),
    ([(200, None)], False), ([(404, "MANIFEST_UNKNOWN"), (200, None)], False),
    ([(401, "UNAUTHORIZED")], False), ([(403, "DENIED")], False),
    ([(404, "DENIED")], False), ([(404, None)], False),
    ([(404, "malformed")], False), ([(0, None)], False),
])
def test_both_registry_tags_must_be_confirmed_absent(monkeypatch, capsys, responses, valid):
    pending = iter(responses)
    calls = []
    private = "fixture-credential-must-not-be-printed"

    def urlopen(request, timeout):
        calls.append(request.full_url)
        assert timeout == 30
        if "/token?" in request.full_url:
            return io.BytesIO(json.dumps({"token": private}).encode())
        status, code = next(pending)
        if status == 200:
            return io.BytesIO(b"{}")
        if status == 0:
            raise OSError(private)
        body = b"invalid JSON" if code == "malformed" else json.dumps(
            {"errors": [{"code": code}]} if code else {}).encode()
        raise urllib.error.HTTPError(request.full_url, status, private, {}, io.BytesIO(body))

    for key, value in {"GITHUB_ACTOR": "fixture", "GH_TOKEN": private, "VERSION": "0.4.1",
                       "IMAGE": "ghcr.io/9vibes/evencomms"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    if valid:
        exec(release_python(1), {})
        assert [url.rsplit("/", 1)[-1] for url in calls[1:]] == ["0.4.1", "0.4.1-codex"]
    else:
        with pytest.raises(SystemExit) as error:
            exec(release_python(1), {})
        assert private not in str(error.value)
    assert len([url for url in calls if "/token?" in url]) == 1
    assert private not in capsys.readouterr().out


@pytest.mark.parametrize("moved_bridge", [False, True])
def test_publication_checks_both_ids_before_tagging(checks, monkeypatch, tmp_path, moved_bridge):
    import container_smoke
    program = release_python(2)
    monkeypatch.chdir(tmp_path)
    for key, value in {"IMAGE": "ghcr.io/9vibes/evencomms", "VERSION": "0.4.1",
                       "GITHUB_REPOSITORY": "9Vibes/EVENCOMMS", "GITHUB_SHA": "c" * 40,
                       "RUNNER_TEMP": str(tmp_path), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "release-image-id").write_text(APP_ID)
    (tmp_path / "release-codex-image-id").write_text(BRIDGE_ID)
    references = {APP_ID: APP_ID, BRIDGE_ID: BRIDGE_ID, "evencomms:release": APP_ID,
                  "evencomms-codex:release": APP_ID if moved_bridge else BRIDGE_ID}
    calls = []

    def image_info(reference, user, labels):
        calls.append(("check", reference))
        assert labels["org.opencontainers.image.version"] == "0.4.1"
        if user == "10002:10002":
            assert labels["org.opencontainers.image.codex-version"] == "0.157.1"
        return references[reference]

    def docker(*command, **kwargs):
        calls.append(command)
        if command[0] == "tag":
            references[command[2]] = command[1]
        elif command[:2] == ("image", "inspect"):
            identity = references[command[2]]
            return json.dumps([{"Id": identity, "RepoDigests": ["ghcr.io/9vibes/evencomms@" + identity]}])
        else:
            assert command[0] == "push"
        return ""

    monkeypatch.setattr(checks, "image_info", image_info)
    monkeypatch.setattr(container_smoke, "docker", docker)
    if moved_bridge:
        with pytest.raises(SystemExit):
            exec(program, {})
        assert all(call[0] == "check" for call in calls)
        return
    exec(program, {})
    assert [call[0] for call in calls[:4]] == ["check"] * 4
    assert [call for call in calls if call[0] == "tag"] == [
        ("tag", APP_ID, "ghcr.io/9vibes/evencomms:0.4.1"),
        ("tag", BRIDGE_ID, "ghcr.io/9vibes/evencomms:0.4.1-codex"),
    ]
    assert (tmp_path / "image-reference.txt").read_text() == "ghcr.io/9vibes/evencomms@" + APP_ID + "\n"
    assert (tmp_path / "codex-image-reference.txt").read_text() == "ghcr.io/9vibes/evencomms@" + BRIDGE_ID + "\n"
    metadata = json.loads((tmp_path / "release-images.json").read_text())
    assert metadata["version"] == "0.4.1" and metadata["platform"] == "linux/amd64"
    assert metadata["images"]["codex"]["image_id"] == BRIDGE_ID


def test_release_upgrade_gates_keep_legacy_checks_and_add_pinned_040():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    pulls = re.findall(r"docker pull (ghcr.io/9vibes/evencomms:[^\s]+)", workflow)
    upgrades = re.findall(r"--upgrade-from (ghcr.io/9vibes/evencomms:[^\s]+)", workflow)
    assert pulls == upgrades
    assert [ref.split(":")[1].split("@")[0] for ref in upgrades] == ["0.1.0", "0.2.1", "0.3.0", "0.4.0"]
    assert upgrades[-1] == (
        "ghcr.io/9vibes/evencomms:0.4.0@sha256:"
        "0b22e2b2d2967e55f244985ebc16cdac3426c852527f83398dce7b639b5e6d15"
    )
    steps = workflow.split("      - name: ")
    upgrade_steps = [step for step in steps if "--upgrade-from " in step]
    for step in upgrade_steps:
        assert '--image "$(cat "$RUNNER_TEMP/release-image-id")"' in step
        assert '--speech-pcm "$RUNNER_TEMP/speech.pcm"' in step
        assert ("--upgrade-private-auth" in step) == (upgrades[-1] in step)
    assert workflow.index("Verify 0.4.0 upgrade") < workflow.index("Preflight BOTH immutable tags")


@pytest.mark.parametrize("prior_auth", [False, True])
def test_upgrade_private_auth_uses_prior_then_candidate_initializer(checks, monkeypatch, prior_auth):
    import container_smoke
    prior_id = "sha256:" + "c" * 64
    initializers = []

    def docker(*command, **kwargs):
        if command[:2] == ("image", "inspect"):
            return json.dumps([{"Id": command[2], "Os": "linux", "Architecture": "amd64",
                                "Config": {"User": "10001:10001"}}])
        if command[0] == "run":
            if command[-1] != container_smoke.INIT_CHECK:
                return "{}"  # Data snapshot before candidate initialization.
            initializers.append(command)
            if command[-4] == APP_ID:
                raise RuntimeError("candidate initializer reached")
        return ""

    args = ["container_smoke", "--image", APP_ID, "--upgrade-from", prior_id]
    if prior_auth:
        args.append("--upgrade-private-auth")
    monkeypatch.setattr(container_smoke.sys, "argv", args)
    monkeypatch.setattr(container_smoke, "docker", docker)
    with pytest.raises(RuntimeError, match="candidate initializer reached"):
        container_smoke.main()
    assert [command[-4] for command in initializers] == ([prior_id, APP_ID] if prior_auth else [APP_ID])
    for command in initializers:
        assert command[command.index("--network") + 1] == "none"
        assert "--pull=never" in command and "--read-only" in command
        assert any("target=/codex-auth,volume-nocopy" in arg for arg in command)
    if prior_auth:
        assert initializers[0][:-4] == initializers[1][:-4]  # Same private volume, never recreated.


def test_upgrade_private_auth_requires_prior_image(checks, monkeypatch):
    import container_smoke
    monkeypatch.setattr(container_smoke.sys, "argv", ["container_smoke", "--image", APP_ID, "--upgrade-private-auth"])
    with pytest.raises(SystemExit) as error:
        container_smoke.main()
    assert error.value.code == 2


@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_docker_failures_suppress_private_diagnostics(checks, monkeypatch, capsys, failure):
    import container_smoke
    private = "do-not-print-private-stdout-stderr-or-stdin"

    def run(command, **kwargs):
        assert kwargs["capture_output"] is True and kwargs["input"] == private
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1, output=private, stderr=private)
        if failure == "oserror":
            raise OSError(private)
        return subprocess.CompletedProcess(command, 1, private, private)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        container_smoke.docker("exec", "owned-container", stdin=private)
    assert private not in str(error.value)
    assert not capsys.readouterr().out


@pytest.fixture
def managed_config(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is needed for config rendering; no daemon is used")
    project = "evencomms-codex-config-test"
    result = subprocess.run(["docker", "compose", "--project-name", project, "--env-file", "/dev/null",
        "-f", str(ROOT / "deploy/umbrel/docker-compose.yml"), "-f", str(ROOT / "deploy/umbrel/ci.override.yml"),
        "config", "--format", "json"], capture_output=True, text=True, timeout=20,
        env={**os.environ, "APP_DATA_DIR": str(tmp_path), "APP_PASSWORD": "fixture-password",
             "OPENAI_API_KEY": "", "CODEX_BRIDGE_TOKEN": "", "COMPOSE_PROFILES": ""})
    assert result.returncode == 0, "Managed Compose did not render"
    return json.loads(result.stdout), project


def test_generated_compose_keeps_source_controls_and_exact_ids(checks, managed_config, tmp_path):
    config, project = managed_config
    generated = checks.prepare_config(config, APP_ID, BRIDGE_ID, project, tmp_path)
    assert generated["services"]["server"]["image"] == APP_ID
    assert generated["services"]["codex-bridge"]["image"] == BRIDGE_ID
    assert generated["services"]["codex-bridge"]["pids_limit"] == 128
    assert int(generated["services"]["codex-bridge"]["mem_limit"]) == 1024**3
    assert generated["services"]["codex-bridge"]["ulimits"]["core"] == 0
    assert set(generated["volumes"]) == {"data", "config", "codex-auth"}
    path = tmp_path / "compose.json"
    path.write_text(json.dumps(generated))
    result = subprocess.run(["docker", "compose", "--env-file", "/dev/null", "-f", str(path), "config", "--quiet"],
                            capture_output=True, timeout=20, env={**os.environ, "ADMIN_PASSWORD": "fixture-password"})
    assert result.returncode == 0, "Generated Compose did not validate"
    for program in (checks.INIT_CHECK, checks.STATUS_CHECK, checks.READY_CHECK, checks.BRIDGE_CHECK,
                    checks.UID101_CHECK, checks.OFFLINE_PROBE):
        compile(program, "container-executed-proof", "exec")


@pytest.mark.parametrize("change", ["bridge_dependency", "direct_token", "host_mount", "unpinned_proxy"])
def test_generated_compose_rejects_unsafe_source_contract(checks, managed_config, tmp_path, change):
    config, project = managed_config
    services = config["services"]
    if change == "bridge_dependency":
        services["server"]["depends_on"]["codex-bridge"] = {"condition": "service_healthy"}
    elif change == "direct_token":
        services["codex-bridge"]["environment"]["CODEX_BRIDGE_TOKEN"] = "0" * 64
    elif change == "host_mount":
        services["codex-bridge"]["volumes"][0]["source"] = "/var/run/docker.sock"
    else:
        services["web"]["image"] = "nginx:latest"
    with pytest.raises(RuntimeError):
        checks.prepare_config(config, APP_ID, BRIDGE_ID, project, tmp_path)


@pytest.fixture
def bridge_inspect():
    return {"Image": BRIDGE_ID, "Config": {"User": "10002:10002"}, "Mounts": [],
            "NetworkSettings": {"Ports": {"8001/tcp": None}}, "HostConfig": {
                "Init": True, "ReadonlyRootfs": True, "CapDrop": ["ALL"], "Privileged": False,
                "SecurityOpt": ["no-new-privileges:true"], "Memory": 1024**3, "MemorySwap": 1024**3,
                "NanoCpus": 10**9, "PidsLimit": 128, "Ulimits": [{"Name": "core", "Soft": 0, "Hard": 0}],
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=256m,mode=1777"}, "PublishAllPorts": False,
                "IpcMode": "private", "NetworkMode": "none"}}


@pytest.mark.parametrize("field,value", [
    ("ReadonlyRootfs", False), ("CapDrop", []), ("SecurityOpt", []), ("MemorySwap", -1),
    ("NanoCpus", 0), ("PidsLimit", 0), ("Ulimits", []), ("Tmpfs", {"/tmp": "rw,size=256m"}),
    ("PortBindings", {"8001/tcp": [{"HostPort": "8001"}]}), ("NetworkMode", "bridge"),
    ("IpcMode", "host"), ("Binds", ["/var/run/docker.sock:/var/run/docker.sock"]),
])
def test_runtime_inspection_rejects_weakened_controls(checks, bridge_inspect, field, value):
    checks.inspect_controls(bridge_inspect, BRIDGE_ID, offline=True)
    changed = copy.deepcopy(bridge_inspect)
    changed["HostConfig"][field] = value
    with pytest.raises(RuntimeError):
        checks.inspect_controls(changed, BRIDGE_ID, offline=True)


def test_private_mount_access_is_inspected(checks):
    mount = {"Destination": "/run/codex-auth", "Type": "volume", "Name": "owned-codex-auth", "RW": False}
    expected = {"/run/codex-auth": ("codex-auth", False)}
    checks.check_mounts({"Mounts": [mount]}, expected, "owned")
    with pytest.raises(RuntimeError):
        checks.check_mounts({"Mounts": [{**mount, "RW": True}]}, expected, "owned")
    with pytest.raises(RuntimeError):
        checks.check_mounts({"Mounts": [mount, {**mount, "Destination": "/data"}]}, expected, "owned")


def test_failed_smoke_cleans_up_only_owned_resources(checks, monkeypatch, tmp_path, capsys):
    from contextlib import nullcontext
    from types import SimpleNamespace
    private = "private-docker-failure-must-not-escape"
    calls = []

    def docker(*command, **kwargs):
        calls.append(command)
        if command[-3:] == ("config", "--format", "json"):
            return "{}"
        if command[0] == "run":
            raise RuntimeError(private)
        return ""

    monkeypatch.setattr(checks, "docker", docker)
    monkeypatch.setattr(checks, "image_info", lambda reference, *args: reference)
    monkeypatch.setattr(checks, "prepare_config", lambda config, *args: config)
    monkeypatch.setattr(checks, "uuid4", lambda: SimpleNamespace(hex="d" * 32))
    monkeypatch.setattr(checks.tempfile, "TemporaryDirectory", lambda **kwargs: nullcontext(str(tmp_path)))
    monkeypatch.setattr(checks.sys, "argv", ["codex_container_smoke", "--image", APP_ID,
        "--codex-image", BRIDGE_ID, "--version", "0.4.1", "--revision", "c" * 40,
        "--source", "https://github.com/9Vibes/EVENCOMMS"])
    with pytest.raises(RuntimeError, match="network-none real Codex production probe") as error:
        checks.main()
    assert private not in str(error.value) and private not in capsys.readouterr().out
    assert calls[-2] == ("rm", "--force", "evencomms-codex-smoke-" + "d" * 12 + "-offline")
    assert calls[-1][:3] == ("compose", "--project-name", "evencomms-codex-smoke-" + "d" * 12)
    assert calls[-1][-5:] == ("down", "--volumes", "--remove-orphans", "--timeout", "15")
