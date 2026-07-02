"""假 server：行式 JSON-RPC echo，供 test_supervisor.py 黑盒测试（非 pytest 用例）。

首次启动自报 generation=1，被换芯重启后 generation=2（用 VIBECAD_FAKE_GEN_FILE
计数文件跨进程区分代际）。协议行为：
- {"method": "swap"}  通知 → os._exit(75)，模拟 server 自退换芯（SWAP_EXIT）；
- {"method": "crash"} 通知 → os._exit(3)，模拟真崩溃（供退出码透传测试）；
- 带 id 的请求 → 回 {"result": {"gen": 代际, "method": 原方法}}；
- stdin EOF → 循环自然走完、以 0 退出（模拟 MCP server 随宿主关闭收尾）。
可选 VIBECAD_FAKE_PID_FILE：落自身 PID，供「无孤儿进程」检测。
可选 VIBECAD_FAKE_SWAP_ON_START：启动即 os._exit(75)——模拟换芯判据两侧不一致
时「新 server 一起来又自杀」的无限重启循环（供 supervisor 循环护栏测试）。
可选 VIBECAD_FAKE_HANG：换芯重启后（gen>=2）收到任何行都不回、不退出——模拟
新子进程握手挂死（conda 首启慢/import 死锁，供 supervisor 握手超时测试）。
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
    if msg.get("method") == "swap":
        os._exit(75)
    if msg.get("method") == "crash":
        os._exit(3)
    if "id" in msg:
        out = {"jsonrpc": "2.0", "id": msg["id"],
               "result": {"gen": gen, "method": msg.get("method")}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
