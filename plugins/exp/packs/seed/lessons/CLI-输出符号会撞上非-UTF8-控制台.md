---
id: CLI-输出符号会撞上非-UTF8-控制台
title: CLI 输出里的特殊符号会撞上非 UTF-8 控制台,把命令直接打断
category: 基架
status: confirmed
severity: medium
trigger: 当你往 CLI 输出里加 ✓/✗/⚠️ 这类符号时;新增任何带符号或 emoji 的打印
related: []
signatures: []
created: '2026-09-19'
---

## 表现

在中文 Windows(默认 GBK 码页)上,打印 `✓ ✗ ⚠️` 这类符号直接抛
`UnicodeEncodeError`,**整个命令中断** —— 用户看到的是你的工具崩了,
而不是"有个检查项没通过"。

## 根因

Python 在 Windows 上默认按系统码页编码 stdout。符号不在 GBK 表里 → 抛异常。
而开发者通常在 UTF-8 环境里写的代码,自己测不出来。

## 做法

1. **启动时统一 stdout/stderr 编码**,失败就退回替换模式:

```python
def _fix_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass          # 老版本 Python 没有 reconfigure,忽略
```

2. 或者**干脆用 ASCII**:`[OK]` / `[!]` / `[x]` 比 ✓✗⚠ 更稳,而且一样清楚。
3. CI 里加一条跨平台冒烟测试,别只在开发机上跑。

## 适用范围

所有跨平台 CLI 工具,尤其是要给人看的输出。
