# 变更记录

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 即将到来
- 卸载后 `.exp/` 的主动提示
- 索引超过容量时的自动分层(目前只是报告)

## [0.1.0] — 2026-09-19

首个可用版本。

### 核心机制

- **两层经验库**:项目级(`<项目>/.exp/`,跟 repo 走)+ 全局级(`~/.exp/`,跟人走)。
  检索时合并,全局降权。
- **机器强制捕获**:失败时由 `PostToolUseFailure` hook 记录,不依赖自觉。
- **注入记账 + 复发归因**:区分 `index`(只看到标题)和 `content`(看到做法)
  两级注入,据此自动算出两个信号:
  - `recurred` —— 看过做法还是犯,说明 fix 不可执行
  - `missed` —— 该命中却没命中,说明触发词写偏了
- **自动降级**:复发到阈值且已确认的经验,自动标记 `needs_rewrite`。
- **索引容量报告**:截断时明确告知,避免"经验静默漏注入"。

### 命令

`init` `hook` `query` `show` `list` `add` `feedback` `distill` `cluster`
`gc` `serve` `migrate` `status` `import` `pack` `stats`

### 工程

- **零依赖**:纯标准库;装了 PyYAML 用 PyYAML,没装走内置 YAML 子集。
  两条路径都在 CI 里真实覆盖。
- **schema 版本号**:每个经验文件带 `schema:` 字段,`exp migrate` 按版本升级
  (默认 dry-run)。
- **解析缓存**:按 `(mtime, size)` 失效。199 条时 hook 耗时从 280ms 降到 ~88ms。
- **跨进程文件锁**:计数读-改-写全程加锁,40 并发写入零丢失。
- **45 个回归测试** × 3 平台 × 2 Python 版本 × 有无 PyYAML。

### 已知边界

- 注入 ≠ 采纳。系统保证经验被记下并送到模型眼前,不保证它采纳 ——
  `recurred` 计数就是用来暴露这个落差的。
- 归因是**相对信号**,不是因果结论。
- 二元组检索匹配不到同义表达,`trigger` 措辞要贴近你实际会想到的词。
- 冷启动期(约前一周)基本无用,价值从第 3-4 周开始显现。
- 捕获层只覆盖"有工具报错"的坑。没有失败事件的坑(如"这段设计不好")
  需要手工 `exp add`。

[未发布]: https://github.com/asdfvbz/agent_exp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/asdfvbz/agent_exp/releases/tag/v0.1.0
