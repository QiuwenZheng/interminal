# Interminal 项目文档

> **个人工具，不是企业级项目。代码做到最轻量精简，不要防御性编程。**

## 架构决策（已确定，不要推翻）

1. **Channel 基类**做 I/O 抽象（鸭子类型，无 ABC/abstractmethod），不用 InteractiveCommand 子类或 SessionProvider ABC
2. **Session dataclass** 直接管理，不要加多余的抽象层
3. **按职责分文件**（channel / command / session_manager），不按 provider 分
4. **`_wait_for_result` 双阶段超时**经过充分验证，不要改动

## 技术要点（不要误改）

- **drain 循环必须保留**（`_read_loop` 中 `is_finished()` 后的 while 循环）—— SSH 必需，本地零开销
- **EOF 差异**：SSH `read()` 返回 `None`，本地返回 `b""`。`elif not has_data` 涵盖两种情形，50ms sleep 无害——下一轮 `is_finished()` 即可退出，不会形成无限空转
- **增量 UTF-8 解码**用 `codecs.getincrementaldecoder('utf-8')('replace')`，处理截断的多字节字符
- **`SSHChannel._stdin` 引用必须保留**：`exec_command` 返回 `(stdin, stdout, stderr)`，若不持有 stdin，Python GC 回收时调用 `stdin.__del__()` → `shutdown_write()` → `channel.eof_sent=True`。paramiko 4.x 在 blocking 模式下 `Channel.send()` 看到 `eof_sent=True` 不抛异常，而是永久阻塞，导致 `sendall` 卡死（respond 永远不返回）。

## pyte 输出清洗

`PYTE_AVAILABLE` 时对**所有**输出（包括 SSH）应用 `_render_pyte()`，不只限于 PtyChannel。
用 `_pyte_accum` 累积每条命令的完整原始输出，每次渲染传入全量文本，保证 pyte Screen 上下文不丢失。

**`_Screen` 子类**：pyte 某版本中 `Stream` 解析 `CSI ? Ps n`（私有 DSR，如 zellij 启动时的终端探测）
时调用 `screen.report_device_status(..., private=True)`，但 `Screen` 方法签名不接受 `private` 参数，
抛 TypeError。这是 pyte 内部 Stream/Screen 未同步的 bug。

修法：用 `_Screen(pyte.Screen)` 子类覆盖该方法，签名为 `(*args, **kwargs)` 且直接 `pass`。
无头模式下 `report_device_status` 本就是空操作（pyte 无法向应用 stdin 回写，且该方法不改变任何
显示状态），完全正确。`_render_pyte` 用 `_Screen` 替换 `pyte.Screen`，无需 try/except 或 regex 降级。

## 已完成的代码审查修复（不要重复做）

- 删除 `channel.py` 未使用的 logging import 和 `is_closed()` 死代码
- 简化 `SSHChannel.get_exit_code()` 和 `LocalChannel.close()`
- 修复 `_read_loop` EOF idle-spin bug
- 清理 `session_manager.py` 多余的 `field` import
- 精简 `mcp_server.py` docstring
- 修复 `PtyChannel.close()` Linux 分支事件循环阻塞 bug：`self._proc.wait()` → `await asyncio.to_thread(self._proc.wait)`
- 修复 Windows `LocalChannel.send_control`：`SIGINT` → `CTRL_BREAK_EVENT` + 子进程用 `CREATE_NEW_PROCESS_GROUP`。原因：Windows subprocess 只接受 `CTRL_C_EVENT`/`CTRL_BREAK_EVENT`；而 `CREATE_NEW_PROCESS_GROUP` 会 disable `CTRL_C_EVENT`，必须用 `CTRL_BREAK_EVENT` 才能真正中断子进程组。Windows `ping -t` 收到 Break 后打印统计并继续运行（这是 ping 自身行为，信号本身已正确送达）。
- 移除 `Channel` ABC 继承与 `@abstractmethod` 装饰器，改用鸭子类型
- `LocalChannel.send_signal` 信号映射表提至模块级 `_LOCAL_SIG_MAP`，避免每次调用重建
- 内联 `RunningCommand._decode` 单行包装方法
- 移除 `write_input` 的 `try/except`，直接抛错
- 删除 `_pyte_accum` 原始文本累积，pyte 每次只渲染当前增量（解决 O(n) 重渲染性能问题）
- 修复 `_read_loop` 本地 EOF 后 CPU 空转：`elif not has_data and raw is None` → `elif not has_data`，EOF（`b""`）不再绕过 sleep 条件形成紧循环
- 修复 `LocalChannel.close()` 和 `PtyChannel.close()`（Linux）超时后遗留孤儿进程：terminate 后等待 2s，超时则升级为 kill 强杀

## 核心卖点

**原生 SSH（paramiko 直连）+ 极致轻量（4 文件 ~700 行）+ partial/respond 交互模式。**

## 已完成的功能升级

### send_control 工具

- Channel ABC 新增 `send_signal(sig: bytes)` 抽象方法
- SSH/PTY：直接写控制字节；LocalChannel（无 PTY）：映射到 OS 信号（SIGINT 等）
- `session_manager.py` 新增 `send_control()` 方法，发送后复用 `_wait_for_result` 收集响应
- MCP 工具参数：`command_id`, `signal="ctrl+c"`（支持 ctrl+c / ctrl+z / ctrl+\）

### SSH 欢迎界面自动捕获

- `connect_ssh` 返回值从 `str` 改为 `dict`：`{"session_id": ..., "banner": ...}`
- 新增 `_capture_ssh_banner(client, banner_timeout)`：`invoke_shell()` → sleep → 读 MOTD → 关闭临时 shell
- `banner_timeout` 由调用方传入（默认 2.0s），临时 shell 不影响后续 `exec_command`

### PTY 扩展

- `channel.py` 新增 `PtyChannel` 类，Windows 用 `pywinpty`，Linux 用 stdlib `pty`
- `PTY_AVAILABLE` 模块级检测，缺依赖时静默降级到 LocalChannel
- `session_manager.py` 中 local 分支：`if PTY_AVAILABLE` 用 PtyChannel，否则原逻辑
- 退出码：Windows `PtyProcess.exitstatus`，Linux `proc.returncode`
- 新增可选依赖：`pywinpty`（Windows）、`pyte`（两平台）

## TUI 多路复用器使用模式

以 zellij 为例，展示 interminal 与长期运行 TUI 的交互范式：

```
# 创建 session（返回 partial，zellij 持续运行）
execute(sid, "zellij --session mytest")   → status=partial, command_id=<cid>

# 用独立的第二条 exec_command 通过 zellij IPC 发送动作
execute(sid, "zellij --session mytest action new-pane")  → "terminal_1"

# 验证
execute(sid, "zellij --session mytest action list-panes")
```

**长期运行进程（TUI、REPL、server）永远返回 partial**，这是正确行为：
进程未退出 → `is_finished()=False` → status="partial" + command_id。
用 `respond` 发键盘输入，用 `send_control` 发信号，用独立 execute 调 CLI 动作接口。

