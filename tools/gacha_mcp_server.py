# -*- coding: utf-8 -*-
"""抽奖工具 MCP Server（Model Context Protocol，stdio 传输）。

不依赖官方 mcp SDK（离线环境装不了），按 MCP 规范实现最小可用子集：
    initialize / notifications/initialized / tools/list / tools/call

协议：JSON-RPC 2.0 over stdio，每行一条 JSON（newline-delimited）。

运行：venv\\Scripts\\python.exe -m tools.gacha_mcp_server
（供 MCP client 连接，如 Claude Desktop / 自定义 agent；agent 本地调用
  直接 import tools.gacha.gacha_pull，无需起进程。）

工具：
    gacha_pull(times: int = 1)  → 抽奖（1=单抽，10=十连），返回文本
"""
from __future__ import annotations

import json
import logging
import sys

from tools.gacha import gacha_pull, get_pool

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {
    "name": "limbus-gacha-server",
    "version": "1.0.0",
}

# ── 工具定义（MCP tools/list 返回的 schema）──
TOOLS = [
    {
        "name": "gacha_pull",
        "description": (
            "边狱巴士（Limbus Company）人格/EGO 抽奖。"
            "概率：三灯人格3%、二灯人格13%、一灯人格81%、EGO 3%。"
            "参数 times：抽取次数（1=单抽，10=十连，1~100）。"
            "返回每抽的灯级与名称（如『三灯人格 · 浮士德黑兽-卯魁首』）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "times": {
                    "type": "integer",
                    "description": "抽取次数：1=单抽，10=十连（1~100）",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 1,
                }
            },
            "additionalProperties": False,
        },
    }
]


def _send(obj: dict):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _text_content(text: str) -> dict:
    return {"type": "text", "text": text}


def _handle_call(name: str, arguments: dict) -> dict:
    """执行工具调用，返回 MCP result。"""
    if name != "gacha_pull":
        return {
            "content": [_text_content(f"未知工具: {name}")],
            "isError": True,
        }
    try:
        times = int((arguments or {}).get("times", 1))
    except (TypeError, ValueError):
        times = 1
    try:
        text = gacha_pull(times)
        return {"content": [_text_content(text)], "isError": False}
    except Exception as e:
        return {"content": [_text_content(f"抽奖失败: {e}")], "isError": True}


def main():
    # 预热池（提前加载数据，避免首抽延迟）
    try:
        pool = get_pool()
        logger.info(
            f"Gacha MCP Server 就绪: 1灯{len(pool._pools['one_star'])} "
            f"2灯{len(pool._pools['two_star'])} 3灯{len(pool._pools['three_star'])} "
            f"EGO{len(pool.ego_items)}"
        )
    except Exception as e:
        logger.error(f"抽奖池预热失败: {e}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = msg.get("id")
        method = msg.get("method") or ""

        # 通知类（无 id，无需响应）
        if msg_id is None:
            continue

        if method == "initialize":
            _send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            })
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            result = _handle_call(
                params.get("name", ""),
                params.get("arguments") or {},
            )
            _send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        else:
            _send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
