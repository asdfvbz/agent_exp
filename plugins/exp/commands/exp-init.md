---
description: 在当前项目启用 exp 经验库(创建 .exp/)
allowed-tools: Bash
---

在当前项目初始化 exp 经验库。

执行:

```bash
python "${CLAUDE_PLUGIN_ROOT}/exp.py" init
```

然后:

1. 确认 `.exp/lessons/` 和 `.exp/config.yaml` 已创建。
2. 跑一次 `exp list` 确认全局层也能读到(`~/.exp/`)。
3. 告诉用户:项目级经验会 commit 进版本库,团队共享;全局级在 `~/.exp/`。

如果用户是从别的工具迁移过来的(比如已有一批 `lessons/*.yaml`),
用 `exp import <目录>` 批量导入,别让用户手工搬。
