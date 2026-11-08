"""MCP（Model Context Protocol）客户端：stdio传输的轻量实现。

通过 .env.local 的 MCP_SERVERS 配置外部工具服务器（JSON数组）：
  MCP_SERVERS=[{"name":"risk-tools","command":["python","-m","my_mcp_server"]}]
加载的MCP工具与本地Function Calling工具统一注册（见core/tools.py），
以 mcp__<server>__<tool> 命名空间暴露给Agent的LLM工具调用。
"""
import json
import subprocess
import threading
from typing import Any, Dict, List, Optional

from credit_risk_system.config.settings import settings
from credit_risk_system.core.logger import get_logger

logger = get_logger("mcp")


class MCPClient:
    """单个MCP服务器客户端（stdio + 换行分隔JSON-RPC 2.0）。"""

    def __init__(self, name: str, command: List[str]):
        self.name = name
        try:
            self.proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except Exception as e:
            raise RuntimeError(f"MCP服务器[{name}]启动失败: {e}") from e
        self._id = 0
        self._lock = threading.Lock()
        # 握手
        self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "credit-risk-system", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._id += 1
            msg = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method,
                              "params": params}, ensure_ascii=False)
            self.proc.stdin.write(msg + "\n")
            self.proc.stdin.flush()
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise ConnectionError(f"MCP服务器[{self.name}]已断开")
                resp = json.loads(line)
                if resp.get("id") == self._id:
                    if "error" in resp:
                        raise RuntimeError(f"MCP调用失败: {resp['error']}")
                    return resp.get("result", {})

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        with self._lock:
            msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params},
                             ensure_ascii=False)
            self.proc.stdin.write(msg + "\n")
            self.proc.stdin.flush()

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._request("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = self._request("tools/call", {"name": tool_name, "arguments": arguments})
        content = result.get("content", [])
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        return {"text": text, "raw": result}

    def close(self) -> None:
        try:
            self.proc.terminate()
        except Exception:
            pass


class MCPToolRef:
    """MCP工具引用。"""
    __slots__ = ("server", "tool", "description", "input_schema")

    def __init__(self, server: MCPClient, tool: Dict[str, Any]):
        self.server = server
        self.tool = tool.get("name", "")
        self.description = tool.get("description", "")
        self.input_schema = tool.get("inputSchema", {"type": "object", "properties": {}})


_mcp_tools: Optional[Dict[str, MCPToolRef]] = None
_lock = threading.Lock()


def get_mcp_tools() -> Dict[str, MCPToolRef]:
    """加载全部MCP工具（懒加载单例，失败时返回空字典不影响主流程）。"""
    global _mcp_tools
    with _lock:
        if _mcp_tools is not None:
            return _mcp_tools
        _mcp_tools = {}
        for server_conf in settings.MCP_SERVERS:
            name = server_conf.get("name", "mcp")
            command = server_conf.get("command", [])
            if not command:
                continue
            try:
                client = MCPClient(name, command)
                for tool in client.list_tools():
                    ref = MCPToolRef(client, tool)
                    _mcp_tools[f"mcp__{name}__{ref.tool}"] = ref
                logger.info("MCP服务器[%s]已连接，加载%d个工具", name, len(_mcp_tools))
            except Exception as e:
                logger.warning("MCP服务器[%s]加载失败: %s", name, e)
        return _mcp_tools


def call_mcp_tool(full_name: str, arguments: Dict[str, Any]) -> str:
    """调用MCP工具。"""
    ref = get_mcp_tools()[full_name]
    return json.dumps(ref.server.call_tool(ref.tool, arguments), ensure_ascii=False)
