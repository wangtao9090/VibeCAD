"""假 server：行式 JSON-RPC echo，供 test_supervisor.py 黑盒测试（非 pytest 用例）。

首次启动自报 generation=1，被换芯重启后 generation=2（用 VIBECAD_FAKE_GEN_FILE
计数文件跨进程区分代际）。协议行为：
- ``VIBECAD_FAKE_SWAP_TOOL=<name>``：gen=1 收到该合法 ``tools/call`` 请求后
  ``os._exit(75)``，模拟响应前换芯；
- ``VIBECAD_FAKE_CRASH_METHOD=<method>``：收到该合法请求后 ``os._exit(3)``，
  模拟真崩溃（供退出码透传测试）；
- ``initialize`` 返回最小正式 ``InitializeResult``；其他带 id 的请求
  返回 {"result": {"gen": 代际, "method": 原方法}}；
- stdin EOF → 循环自然走完、以 0 退出（模拟 MCP server 随宿主关闭收尾）。
可选 VIBECAD_FAKE_PID_FILE：落自身 PID，供「无孤儿进程」检测。
可选 VIBECAD_FAKE_SWAP_ON_START：启动即 os._exit(75)——模拟换芯判据两侧不一致
时「新 server 一起来又自杀」的无限重启循环（供 supervisor 循环护栏测试）。
可选 VIBECAD_FAKE_HANG：换芯重启后（gen>=2）收到任何行都不回、不退出——模拟
新子进程握手挂死（conda 首启慢/import 死锁，供 supervisor 握手超时测试）。
可选 VIBECAD_FAKE_TOOL_LOG：每个 ``tools/call`` 在响应/退出前追加一行
``<gen>:<tool-name>``，供精确证明不安全请求没有跨换芯重放。
"""

import json
import os
import sys
import time
from pathlib import Path

gen_file = Path(os.environ["VIBECAD_FAKE_GEN_FILE"])
gen = int(gen_file.read_text()) + 1 if gen_file.exists() else 1
gen_file.write_text(str(gen))

if pid_file := os.environ.get("VIBECAD_FAKE_PID_FILE"):
    Path(pid_file).write_text(str(os.getpid()))

if os.environ.get("VIBECAD_FAKE_SWAP_ON_START"):
    os._exit(75)

_hang = bool(os.environ.get("VIBECAD_FAKE_HANG")) and gen >= 2

for line in sys.stdin:
    if _hang:  # 收到重放的 initialize：不回、不退，模拟握手挂死
        while True:
            time.sleep(3600)
    msg = json.loads(line)
    method = msg.get("method")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    tool_name = params.get("name") if method == "tools/call" else None
    if tool_name is not None and (tool_log := os.environ.get("VIBECAD_FAKE_TOOL_LOG")):
        with Path(tool_log).open("a", encoding="utf-8") as stream:
            stream.write(f"{gen}:{tool_name}\n")
    crash_method = os.environ.get("VIBECAD_FAKE_CRASH_METHOD")
    if crash_method and method == crash_method:
        os._exit(3)
    swap_tool = os.environ.get("VIBECAD_FAKE_SWAP_TOOL")
    if gen == 1 and swap_tool and tool_name == swap_tool:
        os._exit(75)
    if "id" in msg:
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {},
                "serverInfo": {"name": "vibecad-fake", "version": str(gen)},
            }
        else:
            result = {"gen": gen, "method": method, "tool": tool_name}
        out = {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": result,
        }
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
