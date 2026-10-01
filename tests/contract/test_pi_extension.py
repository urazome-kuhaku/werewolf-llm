"""Contract checks for the Pi 0.87.1 knowledge extension.

The test uses a loopback aiohttp server and Pi's own extension loader. It
never starts a model turn, so the check does not consume model quota.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "extensions" / "werewolf_knowledge.ts"


def _pi_package_dir() -> Path | None:
    candidates = [
        Path(value)
        for value in (
            os.environ.get("WEREWOLF_PI_CODING_AGENT_DIR"),
            r"C:\nvm4w\nodejs\node_modules\@earendil-works\pi-coding-agent",
        )
        if value
    ]
    node = shutil.which("node")
    if node:
        candidates.append(
            Path(node).resolve().parent / "node_modules" / "@earendil-works" / "pi-coding-agent"
        )
    for candidate in candidates:
        if (candidate / "package.json").is_file() and (
            candidate / "dist" / "core" / "extensions" / "loader.js"
        ).is_file():
            return candidate
    return None


def _node_executable() -> str | None:
    return shutil.which("node")


async def _run_extension(
    *,
    package_dir: Path,
    node: str,
    base_url: str,
    token: str,
    params: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    script = r"""
import { loadExtensions } from "./dist/core/extensions/loader.js";

const loaded = await loadExtensions([process.env.WEREWOLF_EXTENSION], process.env.WEREWOLF_CWD);
const extension = loaded.extensions.find(
  (item) => item.resolvedPath === process.env.WEREWOLF_EXTENSION
);
if (!extension) throw new Error(`extension was not loaded: ${JSON.stringify(loaded.errors)}`);
const names = [...extension.tools.keys()];
const results = {};
for (const [name, args] of Object.entries(JSON.parse(process.env.WEREWOLF_PARAMS))) {
  const registered = extension.tools.get(name);
  if (!registered) throw new Error(`missing registered tool: ${name}`);
  results[name] = await registered.definition.execute(
    `contract_${name}`, args, undefined, undefined, {}
  );
}
process.stdout.write(JSON.stringify({ names, results }));
"""
    environment = os.environ.copy()
    environment.update(
        {
            "WEREWOLF_EXTENSION": str(EXTENSION),
            "WEREWOLF_CWD": str(ROOT),
            "WEREWOLF_PARAMS": json.dumps(params),
            "WEREWOLF_KNOWLEDGE_BASE_URL": base_url,
            "WEREWOLF_KNOWLEDGE_TOKEN": token,
        }
    )
    completed = await asyncio.to_thread(
        subprocess.run,
        [node, "--input-type=module", "-e", script],
        cwd=package_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


@pytest.mark.asyncio
async def test_pi_extension_registers_seven_tools_and_maps_all_gateway_routes() -> None:
    package_dir = _pi_package_dir()
    node = _node_executable()
    if package_dir is None or node is None:
        pytest.skip("Pi 0.87.1 is not installed on this host")

    requests: list[tuple[str, str, dict[str, Any] | None, str | None]] = []

    async def handle(request: web.Request) -> web.Response:
        raw = await request.read()
        payload = json.loads(raw) if raw else None
        requests.append(
            (request.method, request.path, payload, request.headers.get("Authorization"))
        )
        return web.json_response({"status": "ok", "route": request.path})

    app = web.Application()
    app.router.add_get("/v1/game/skills/me", handle)
    app.router.add_get("/v1/board/{id}", handle)
    app.router.add_get("/v1/role/{id}", handle)
    app.router.add_get("/v1/mechanic/{id}", handle)
    app.router.add_get("/v1/topic/{id}", handle)
    app.router.add_post("/v1/interactions/query", handle)
    app.router.add_post("/v1/search", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]

    params = {
        "get_board": {"id": "classic"},
        "get_role": {"role_id": "witch"},
        "get_mechanic": {"mechanic_id": "voting"},
        "get_interaction": {"subjects": ["witch", "hunter"], "situation": "witch.poison"},
        "get_rule_topic": {"topic_id": "overview"},
        "search_rules": {"query": "witch", "kinds": ["role"], "limit": 3},
        "get_skill_status": {},
    }
    try:
        output = await _run_extension(
            package_dir=package_dir,
            node=node,
            base_url=f"http://127.0.0.1:{port}/v1",
            token="seat-scoped-test-token",
            params=params,
        )
    finally:
        await runner.cleanup()

    assert output["names"] == list(params)
    assert [item[:2] for item in requests] == [
        ("GET", "/v1/board/classic"),
        ("GET", "/v1/role/witch"),
        ("GET", "/v1/mechanic/voting"),
        ("POST", "/v1/interactions/query"),
        ("GET", "/v1/topic/overview"),
        ("POST", "/v1/search"),
        ("GET", "/v1/game/skills/me"),
    ]
    assert [item[3] for item in requests] == ["Bearer seat-scoped-test-token"] * 7
    assert requests[3][2] == params["get_interaction"]
    assert requests[5][2] == params["search_rules"]
    assert requests[6][2] is None
    assert all(result["details"]["ok"] for result in output["results"].values())


@pytest.mark.asyncio
async def test_pi_extension_preserves_interaction_recovery_details() -> None:
    package_dir = _pi_package_dir()
    node = _node_executable()
    if package_dir is None or node is None:
        pytest.skip("Pi 0.87.1 is not installed on this host")

    async def handle(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response(
            {
                "status": "error",
                "error": {
                    "code": "NOT_FOUND",
                    "message": "no exact interaction matches",
                    "details": {
                        "next_tool": "search_rules",
                        "next_tool_args": {"kinds": ["interaction"], "limit": 4},
                        "candidates": [
                            {
                                "ref": "interaction:witch-poison-hunter@1.0.0",
                                "subjects": ["witch_poison", "hunter"],
                                "situation_key": "witch.poison_hunter",
                            }
                        ],
                    },
                },
            },
            status=404,
        )

    app = web.Application()
    app.router.add_post("/v1/interactions/query", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    try:
        output = await _run_extension(
            package_dir=package_dir,
            node=node,
            base_url=f"http://127.0.0.1:{port}/v1",
            token="seat-scoped-test-token",
            params={"get_interaction": {"subjects": ["witch", "hunter"]}},
        )
    finally:
        await runner.cleanup()

    result = output["results"]["get_interaction"]
    assert result["details"]["ok"] is False
    assert result["details"]["error"]["code"] == "NOT_FOUND"
    assert result["details"]["error"]["details"]["next_tool"] == "search_rules"
    assert result["details"]["error"]["details"]["candidates"][0]["situation_key"] == (
        "witch.poison_hunter"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("board_params", [{}, {"id": ""}, {"id": "   "}])
async def test_pi_extension_get_board_uses_current_route_for_blank_ids(
    board_params: dict[str, Any],
) -> None:
    package_dir = _pi_package_dir()
    node = _node_executable()
    if package_dir is None or node is None:
        pytest.skip("Pi 0.87.1 is not installed on this host")

    requests: list[tuple[str, str, dict[str, Any] | None, str | None]] = []

    async def handle(request: web.Request) -> web.Response:
        raw = await request.read()
        payload = json.loads(raw) if raw else None
        requests.append(
            (request.method, request.path, payload, request.headers.get("Authorization"))
        )
        return web.json_response({"status": "ok", "route": request.path})

    app = web.Application()
    app.router.add_get("/v1/board/{id}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]

    try:
        output = await _run_extension(
            package_dir=package_dir,
            node=node,
            base_url=f"http://127.0.0.1:{port}/v1",
            token="seat-scoped-test-token",
            params={"get_board": board_params},
        )
    finally:
        await runner.cleanup()

    assert requests == [("GET", "/v1/board/current", None, "Bearer seat-scoped-test-token")]
    assert output["results"]["get_board"]["details"]["ok"] is True


@pytest.mark.asyncio
async def test_pi_extension_rejects_non_loopback_configuration() -> None:
    package_dir = _pi_package_dir()
    node = _node_executable()
    if package_dir is None or node is None:
        pytest.skip("Pi 0.87.1 is not installed on this host")

    output = await _run_extension(
        package_dir=package_dir,
        node=node,
        base_url="http://localhost:9999/v1",
        token="seat-scoped-test-token",
        params={"get_board": {"id": "classic"}},
    )
    assert output["names"] == [
        "get_board",
        "get_role",
        "get_mechanic",
        "get_interaction",
        "get_rule_topic",
        "search_rules",
        "get_skill_status",
    ]
    result = output["results"]["get_board"]
    assert result["details"]["ok"] is False
    assert result["details"]["error"]["code"] == "CONFIGURATION_ERROR"
