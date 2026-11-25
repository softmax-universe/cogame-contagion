"""One native Contagion game with canned, prompt, and scripted player processes."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar


class ModelStub(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str]]] = []
    reply_text: ClassVar[str] = '{"lockdown":1,"testing":2,"borders":{},"aid":[],"say":"","notes":""}'
    use_fence_on_odd: ClassVar[bool] = True

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        slot = self.headers["X-Coworld-Player-Slot"]
        self.calls.append((self.path, slot))
        assert self.path == "/v1/messages"
        assert "Maximize your own ledger minus twice your deaths. True infections" in request["system"]
        text = self.reply_text
        if self.use_fence_on_odd and len(self.calls) % 2 == 1:
            text = f"```json\n{text}\n```"
        payload = {"content": [{"type": "text", "text": text}]}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        pass


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> None:
    game_bin, player_bin = map(Path, sys.argv[1:3])
    game_port = free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", 0), ModelStub)
    stub_url = f"http://127.0.0.1:{stub.server_port}"
    thread = threading.Thread(target=stub.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="contagion-mixed-") as temp:
        work = Path(temp)
        config = {
            "seed": 7,
            "promptVariant": "mixed",
            "promptSeed": 4,
            "weeks": 4,
            "turnDelayMs": 0,
            "turnBudgetSeconds": 5,
            "player_connect_timeout_seconds": 15,
            "episodeTimeoutSeconds": 120,
            "players": [{"name": f"P{seat}"} for seat in range(6)],
            "tokens": [f"t{seat}" for seat in range(6)],
        }
        (work / "config.json").write_text(json.dumps(config))
        game_env = os.environ.copy()
        for key in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY_URI",
            "COWORLD_LLM_ENDPOINT",
            "AWS_BEARER_TOKEN_BEDROCK",
        ):
            game_env.pop(key, None)
        game_env.update(
            {
                "COGAME_HOST": "127.0.0.1",
                "COGAME_PORT": str(game_port),
                "COGAME_CONFIG_URI": (work / "config.json").as_uri(),
                "COGAME_RESULTS_URI": (work / "results.json").as_uri(),
                "COGAME_SAVE_REPLAY_URI": (work / "replay.json").as_uri(),
            }
        )
        processes = []
        logs = []
        try:
            game_log = (work / "game.log").open("w+")
            logs.append(game_log)
            game = subprocess.Popen(
                [str(game_bin)], env=game_env, stdout=game_log, stderr=subprocess.STDOUT
            )
            processes.append(game)
            for _ in range(100):
                if game.poll() is not None:
                    raise RuntimeError("game exited before healthz")
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    ready = probe.connect_ex(("127.0.0.1", game_port)) == 0
                if ready:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{game_port}/healthz", timeout=0.2
                    )
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("game healthz did not start")
            for seat in range(6):
                env = game_env.copy()
                env["COWORLD_PLAYER_WS_URL"] = (
                    f"ws://127.0.0.1:{game_port}/player?slot={seat}&token=t{seat}"
                )
                if seat == 1:
                    env["COWORLD_LLM_ENDPOINT"] = stub_url
                if seat == 0:
                    env["POC_CAPTURE_TRAINING"] = "1"
                    env["POC_SOURCE_REVISION"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
                    env["COWORLD_PLAYER_ARTIFACT_UPLOAD_URL"] = (work / "policy_artifact_0.zip").as_uri()
                    argv = [
                        sys.executable,
                        str(Path(__file__).parents[2] / "players/ordinary/player.py"),
                    ]
                else:
                    if seat >= 2:
                        env["PLAYER_SCRIPTED"] = "sentinel"
                    argv = [str(player_bin)]
                handle = (work / f"player-{seat}.log").open("w+")
                logs.append(handle)
                processes.append(
                    subprocess.Popen(
                        argv, env=env, stdout=handle, stderr=subprocess.STDOUT
                    )
                )
            assert game.wait(timeout=60) == 0
            for player in processes[1:]:
                assert player.wait(timeout=10) == 0
            results = json.loads((work / "results.json").read_text())
            replay = json.loads((work / "replay.json").read_text())
            assert results["weeks"] == 4 and replay["events"]
            with zipfile.ZipFile(work / "policy_artifact_0.zip") as artifact:
                trajectory = json.loads(artifact.read("trajectory.json"))
            assert trajectory["complete"] and len(trajectory["decisions"]) == 4
            for row in trajectory["decisions"]:
                metadata = row["prompt_template"]
                assert metadata["variant"] == "reordered"
                assert row["execution_evidence"]["accepted"]
                assert json.loads(row["completion"][0]["content"]) == row["execution_evidence"]["executed_action"]
                for message in row["prompt"]:
                    assert metadata[message["role"] + "_sha256"] == hashlib.sha256(message["content"].encode()).hexdigest()
            game_text = (work / "game.log").read_text()
            assert "using sentinel fallback" not in game_text
            prompt = [call for call in ModelStub.calls if call[0] == "/v1/messages"]
            assert len(prompt) == 4 and all(slot == "1" for _, slot in prompt)
            seat1_dials = [
                ev for ev in replay["events"]
                if ev["kind"] == "dial" and ev["seat"] == 1
            ]
            assert len(seat1_dials) == 4
            assert all(
                ev["lockdown"] == 1 and ev["testing"] == 2 and not ev["scripted"]
                for ev in seat1_dials
            )
            print(
                "mixed episode: 4 canned, 4 prompt (incl. markdown fence), 16 scripted decisions; zero game fallback"
            )
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            for handle in logs:
                handle.close()
            stub.shutdown()

    verify_negative_cases(game_bin, player_bin)


def verify_negative_cases(game_bin: Path, player_bin: Path) -> None:
    """Verify missing credentials and invalid model replies fail explicitly without fabricating results."""
    # Case 1: missing credentials on contagion-player -> explicit player_failure.json, no results.json
    game_port = free_port()
    with tempfile.TemporaryDirectory(prefix="contagion-nocreds-") as temp:
        work = Path(temp)
        config = {
            "seed": 7, "weeks": 4, "turnDelayMs": 0, "turnBudgetSeconds": 5,
            "player_connect_timeout_seconds": 3, "episodeTimeoutSeconds": 60,
            "players": [{"name": f"P{seat}"} for seat in range(6)],
            "tokens": [f"t{seat}" for seat in range(6)],
        }
        (work / "config.json").write_text(json.dumps(config))
        base_env = os.environ.copy()
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_URI", "COWORLD_LLM_ENDPOINT",
                    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "AWS_BEARER_TOKEN_BEDROCK"):
            base_env.pop(key, None)
        game_env = base_env.copy()
        game_env.update({
            "COGAME_HOST": "127.0.0.1", "COGAME_PORT": str(game_port),
            "COGAME_CONFIG_URI": (work / "config.json").as_uri(),
            "COGAME_RESULTS_URI": (work / "results.json").as_uri(),
            "COGAME_SAVE_REPLAY_URI": (work / "replay.json").as_uri(),
            "COGAME_PLAYER_FAILURE_URI": (work / "player_failure.json").as_uri(),
        })
        procs = []
        try:
            game = subprocess.Popen([str(game_bin)], env=game_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            procs.append(game)
            for _ in range(100):
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    if probe.connect_ex(("127.0.0.1", game_port)) == 0:
                        break
                time.sleep(0.05)
            p0 = None
            for seat in range(6):
                env = base_env.copy()
                env["COWORLD_PLAYER_WS_URL"] = f"ws://127.0.0.1:{game_port}/player?slot={seat}&token=t{seat}"
                if seat != 0:
                    env["PLAYER_SCRIPTED"] = "sentinel"
                proc = subprocess.Popen([str(player_bin)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                procs.append(proc)
                if seat == 0:
                    p0 = proc
            assert p0 is not None and p0.wait(timeout=15) != 0, "unconfigured model player must exit nonzero"
            assert game.wait(timeout=15) == 0
            assert not (work / "results.json").exists(), "results.json must not be fabricated on missing credentials"
            assert not (work / "replay.json").exists(), "replay.json must not be fabricated on missing credentials"
            failure = json.loads((work / "player_failure.json").read_text())
            assert failure["failed_policy_index"] == 0
            assert failure["message"]
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)

    # Case 2: invalid model reply (out-of-range lockdown) -> rejected, raw preserved, no results.json
    class BadModelStub(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            _ = self.rfile.read(int(self.headers["Content-Length"]))
            payload = {"content": [{"type": "text", "text": '{"lockdown":9,"testing":2,"borders":{},"aid":[],"say":"bad","notes":""}'}]}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: object) -> None:
            pass

    bad_stub = ThreadingHTTPServer(("127.0.0.1", 0), BadModelStub)
    threading.Thread(target=bad_stub.serve_forever, daemon=True).start()
    game_port = free_port()
    with tempfile.TemporaryDirectory(prefix="contagion-badreply-") as temp:
        work = Path(temp)
        config = {
            "seed": 7, "weeks": 4, "turnDelayMs": 0, "turnBudgetSeconds": 5,
            "player_connect_timeout_seconds": 10, "episodeTimeoutSeconds": 60,
            "players": [{"name": f"P{seat}"} for seat in range(6)],
            "tokens": [f"t{seat}" for seat in range(6)],
        }
        (work / "config.json").write_text(json.dumps(config))
        base_env = os.environ.copy()
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_URI", "COWORLD_LLM_ENDPOINT",
                    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "AWS_BEARER_TOKEN_BEDROCK"):
            base_env.pop(key, None)
        game_env = base_env.copy()
        game_env.update({
            "COGAME_HOST": "127.0.0.1", "COGAME_PORT": str(game_port),
            "COGAME_CONFIG_URI": (work / "config.json").as_uri(),
            "COGAME_RESULTS_URI": (work / "results.json").as_uri(),
            "COGAME_SAVE_REPLAY_URI": (work / "replay.json").as_uri(),
            "COGAME_PLAYER_FAILURE_URI": (work / "player_failure.json").as_uri(),
        })
        procs = []
        try:
            game = subprocess.Popen([str(game_bin)], env=game_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            procs.append(game)
            for _ in range(100):
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    if probe.connect_ex(("127.0.0.1", game_port)) == 0:
                        break
                time.sleep(0.05)
            for seat in range(6):
                env = base_env.copy()
                env["COWORLD_PLAYER_WS_URL"] = f"ws://127.0.0.1:{game_port}/player?slot={seat}&token=t{seat}"
                if seat == 0:
                    env["COWORLD_LLM_ENDPOINT"] = f"http://127.0.0.1:{bad_stub.server_port}"
                else:
                    env["PLAYER_SCRIPTED"] = "sentinel"
                procs.append(subprocess.Popen([str(player_bin)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
            assert game.wait(timeout=15) == 0
            assert not (work / "results.json").exists(), "results.json must not be fabricated on invalid action"
            assert not (work / "replay.json").exists(), "replay.json must not be fabricated on invalid action"
            failure = json.loads((work / "player_failure.json").read_text())
            assert failure["failed_policy_index"] == 0
            assert '"lockdown":9' in failure["message"]
            print("negative checks: missing credentials and invalid reply both failed explicitly without fallback")
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            bad_stub.shutdown()


if __name__ == "__main__":
    main()
