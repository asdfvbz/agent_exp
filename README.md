# exp

> 给编程 agent 的经验库。
> 它不只是记住踩过的坑 —— **它知道哪些坑记住了也没用。**

[![test](https://github.com/asdfvbz/agent_exp/actions/workflows/test.yml/badge.svg)](https://github.com/asdfvbz/agent_exp/actions/workflows/test.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#为什么零依赖)

---

## 它做什么

Agent 踩了个坑 → 自动记下来 → 下次开工自动出现在它眼前 →
**如果它还是犯了,这条经验会被自动标记为"坏的"。**

最后那句才是重点。大多数记忆工具解决"怎么存",而真正的失败是
**坏经验持续污染每一次注入,却没人知道**。

```console
$ exp gc
合计 5 条(项目 2 / 全局 3)
确认 1 · 未验证 3 · 已推翻 0

── 反复复发 (1) —— 注入了还是犯,fix 多半不可执行 ──
  阈值必须有来源
    注入3 复发2

── 检索漏检 (1) —— 触发词写偏了,该命中没命中 ──
  不要用 var 声明变量  (漏检3)
    现在写的触发词: 当你写 JavaScript 变量声明时

处置建议:
  复发 → 重写 fix,让它可执行;或降级为 hypothesis
  漏检 → 重写 trigger,用你实际会想到的词
  死重 → 删掉,或合并进相近的经验
```

这一页就是你用一周后最该看的东西:**哪些经验在起作用,哪些是废的。**

---

## 为什么需要它

**你的 `CLAUDE.md` 正在腐烂,而没有任何东西能告诉你。**

它会一直长,没人修剪;它说的对不对,无从检验。三个月后里面
一半是过期的、一半是没人验证过的猜测 —— 而读它的 agent 无法分辨。

经验库不是用来替代 linter 和测试的。**能立刻跑测试发现的坑不需要记 ——
agent 两秒就知道了。** 它值钱的地方是失败**不报错**的时候:

| 坑 | 为什么 linter / 测试抓不到 |
|---|---|
| YAML 里的星号 | **静默**毁掉解析,报错指向别处 |
| 阈值没有来源 | 没人会回头质疑一个已经写进配置的数字 |
| LLM-as-judge 的评分 | 看起来像客观事实,其实是带噪声的信号 |

> 「这段代码用 `var` 不好」→ 该进 linter
> 「这个仓库的 API 要传 `user_id`」→ 该进 CLAUDE.md
> **「阈值必须有来源」→ 只有经验库能装**

---

## 安装

需要 **Python 3.8+**。零第三方依赖,不需要 `pip install`。

### 一、作为 Claude Code 插件(推荐)

```bash
/plugin marketplace add asdfvbz/agent_exp
/plugin install exp@exp
```

然后让 `exp` 命令可用 —— 插件不会自动提供可执行文件:

```bash
export PATH="$PATH:<插件目录>/bin"
```

不确定插件装在哪?在 Claude Code 里跑 `/plugin`,它会显示路径。
Windows 用同目录的 `exp.cmd`;或者用别名:

```bash
alias exp='python3 <插件目录>/exp.py'   # 只有 python 就改成 python
```

### 二、手动接 hooks(不用插件系统)

clone 之后把这三条加进 `~/.claude/settings.json`。

注意命令是 **`python3 ... || python ...`** 的兜底形式 ——
macOS / Linux 上通常只有 `python3`,Windows 上通常只有 `python`,
写死任何一个都会让 hook 在另一半环境里静默失效。

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command",
        "command": "python3 /path/to/exp/plugins/exp/exp.py hook session-start || python /path/to/exp/plugins/exp/exp.py hook session-start" } ] }
    ],
    "PostToolUseFailure": [
      { "hooks": [ { "type": "command",
        "command": "python3 /path/to/exp/plugins/exp/exp.py hook post-failure || python /path/to/exp/plugins/exp/exp.py hook post-failure" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command",
        "command": "python3 /path/to/exp/plugins/exp/exp.py hook stop || python /path/to/exp/plugins/exp/exp.py hook stop" } ] }
    ]
  }
}
```

### 三、只要命令行

不要 hook,当个普通 CLI 用:

```bash
git clone https://github.com/asdfvbz/agent_exp && export PATH="$PATH:$PWD/agent_exp/plugins/exp/bin"
```

**这一档丢掉捕获和自动注入** —— 也就是"机器强制"的部分,退化成
"靠 agent 自觉"。仍然比没有强,但你得知道它弱在哪。

---

## 快速开始

```bash
cd 你的项目
exp init                              # 创建 .exp/ —— 这一步才激活机制
exp pack install seed                 # 装 8 条跨项目通用的坑(可选,但建议)
```

`.exp/lessons/` **建议 commit 进版本库** —— 一个人踩的坑,全队都不用再踩。

```bash
# 记录一条
exp add "改了 schema 必须同步改 migration" \
    --trigger "当你修改数据库 schema 时" \
    --root-cause "两个真相源,没有东西保证它们一致" \
    --fix "改完 schema 立刻跑 make gen,它会重新生成 migration" \
    --severity high

# 开工前查
exp query "我要加一个字段"

# 体检
exp gc
```

---

## 它是怎么知道哪条经验是坏的

这是整个项目唯一有技术含量的部分。

每次注入都记账,并且**区分两个层级**:

| 层级 | 什么时候 | 模型看到了什么 |
|---|---|---|
| `index` | 会话启动 | 标题 + 触发词 |
| `content` | `exp query` / `exp show` | 根因 + **做法** |

失败发生时,按**签名**(工具 + 错误码 + 归一化后的错误信息)归因:

| 信号 | 判据 | 说明什么 | 怎么修 |
|---|---|---|---|
| **`recurred`** | **看过做法**还是犯了 | fix 不可执行 | 重写 fix |
| **`missed`** | 该命中却没到手上 | 触发词写偏了 | 重写 trigger |

**为什么必须区分层级:** 只看过标题就犯,不算"fix 没用" ——
那是模型判断这条跟自己无关,属于触发词的问题,应该记 `missed`。

复发到阈值(`RECUR_THRESHOLD`,默认 2)且已确认的经验,
会被**自动降级**为 `needs_rewrite`。

两个信号**全是机器算的,不需要人工标注**,而且**不要求你提前做任何标注** ——
即使这条经验没挂签名,系统也会用失败信息本身去比对:

| 相关度 | 该经验注入过? | 记为 |
|---|---|---|
| 高(重合 ≥ 4) | ✅ 看过做法 | `recurred` |
| 高(重合 ≥ 4) | ❌ 没到手上 | `missed` |

> **签名是可选的加速器,不是前提。** 挂了签名(从 `exp distill` 的输出里抄)
> 能精确匹配到结构相同的失败;没挂也能靠失败信息本身的措辞比对 ——
> 只是精度低一些。

---

## 装完之后自动发生什么

项目里跑过 `exp init` 之后,这三件事**不依赖任何人的自觉**:

| 时机 | Hook | 动作 |
|---|---|---|
| 会话开始 | `SessionStart` | 注入经验索引(标题 + 触发词) |
| 工具失败 | `PostToolUseFailure` | 算签名 → 归因 → 落原始事件 |
| 回合结束 | `Stop` | 把重复失败归拢成候选 |

**全部零 LLM 调用。** 归纳留给你自己 —— 你有当前会话的完整语境,
比任何外部调用都更懂当时发生了什么。

> 插件是**全局安装**的,但机制只在有 `.exp/` 的项目里生效。
> 无关项目里它一声不响(否则会到处捕获噪声)。

---

## 两层

| 层 | 位置 | 跟着谁走 | 什么时候用 |
|---|---|---|---|
| **项目级** | `<项目>/.exp/` | repo | 只对这个仓库成立的经验。**要 commit。** |
| **全局级** | `~/.exp/` | 人 / 机器 | 换任何仓库都成立的经验。跨项目复利。 |

检索时自动合并,全局层降权。判断标准:

| 经验 | 放哪层 |
|---|---|
| YAML 里的星号会静默毁掉解析 | **全局** |
| 阈值必须有来源 | **全局** |
| 本仓库的测试要先起 `docker-compose` | 项目级 |
| 这个 API 的分页从 1 开始 | 项目级 |

```bash
exp add "..." --layer global
```

---

## 命令

| 命令 | 做什么 |
|---|---|
| `exp query "<我要做什么>"` | 检索(最常用) |
| `exp show <id>` | 看全文,末尾附**算出来的**相关经验 |
| `exp list` | 列出,**按健康度分组** |
| `exp add` | 记一条 |
| `exp gc` | 体检:反复复发 / 检索漏检 / 死重 |
| `exp cluster` | 按触发词聚类,找可合并的重复 |
| `exp feedback <id>` | 显式反馈 `helped` / `recurred` / `confirmed` / `refuted` |
| `exp serve` | 网页面板 |
| `exp status` | 安装位置、数据位置、卸载指引 |
| `exp migrate` | 升级经验文件的 schema(默认 dry-run) |
| `exp pack list\|show\|install` | 经验包 |
| `exp distill` | 把原始失败事件归拢成候选 |
| `exp import <目录>` | 从别的格式迁移 |

常用筛选:

```bash
exp list --health rotten     # 只看反复复发的
exp list --health missed     # 只看检索漏检的
exp list --layer global      # 只看全局层
exp list --by category       # 按主题分组(仅用于人工翻阅)
```

---

## 网页面板

```bash
exp serve        # → http://127.0.0.1:8765
```

零依赖(标准库 `http.server` + 一个静态页,**没有构建步骤**)。
一个需要 `npm install` 的可视化工具,在你想看它的时候多半是坏的。

面板上每条经验都带健康度徽章和 **注入 / 复发 / 漏检** 计数,
点开是全文;可按健康度或相似度分组,支持搜索与筛选。

> 健康度不只用颜色区分,还配了**字形** `● ▲ ◆ ■` ——
> 色盲或灰度打印下依然可读。

---

## 经验包

一条项目私有的经验对别人没用。但**跨项目通用的坑,别人的经验就是你的经验**:

```bash
exp pack list
exp pack show seed
exp pack install seed        # 默认装到全局层
```

自带 [`seed`](plugins/exp/packs/seed/) 包:8 条跨项目通用的坑(YAML 静默失败、阈值来源、
LLM 评分噪声、度量工具覆盖、派生文件同步、语义哈希、聚合指标误用、CLI 编码)。

**这是这个项目能积累出统计意义的前提** —— 一个人的笔记本永远攒不到。
想贡献经验包见 [`packs/README.md`](plugins/exp/packs/README.md)。

---

## 诚实的边界

这个项目**不假装**自己解决了所有问题。以下是已知的:

- **注入 ≠ 采纳。** 系统保证经验被记下并送到模型眼前,**不保证它采纳**。
  `recurred` 计数就是用来暴露这个落差的 —— 它高,说明记了也没用。
- **归因是相对信号,不是因果结论。** 它便宜、全自动、方向正确,
  但它不知道"这次失败是不是因为别的原因"。
- **二元组检索匹配不到同义表达。** 经验写"阈值",你查"标准值",不会命中。
  所以 `trigger` 措辞要贴近**你实际会在场景里想到的词**。`missed` 就是用来暴露它的。
- **冷启动期基本无用。** 第一周不会有任何变化,原始事件还没攒够。
  价值从第 3-4 周开始显现。
- **捕获层只覆盖"有工具报错"的坑。** 没有失败事件的坑(如"这段设计不好")
  需要手工 `exp add`。
- **引擎领域无关,但承诺只给编程域。** 理由见
  [`docs/DESIGN.md`](docs/DESIGN.md#为什么定位是编程)。

---

## 排障

**装好了但什么都没发生**
先确认当前项目跑过 `exp init` —— `.exp/` 才是激活开关。

```bash
ls -d .exp && exp list
```

**hooks 报 `python: command not found`(或 `python3: command not found`)**
`hooks/hooks.json` 里的命令已经写成 `python3 ... || python ...` 的兜底形式,
两个名字哪个存在都能跑。如果两个都不存在(比如只有 `py`),
把 `command` 换成你的解释器路径。

**经验没被记下来**
`PostToolUseFailure` 只在**工具调用失败**时触发。如果你的坑没有任何工具
报错,它捕获不到 —— 那类经验得手工 `exp add`。

**想让模型更可能采纳经验**
把 `--fix` 写得**可执行**。「注意保持同步」不是 fix;
「改完 schema 立刻跑 `make gen`」才是。判断方法:
**照着这句话做,能不能机械化地判断做没做到?**

---

## 数据安全

计数器读不出来时**会先从备份恢复**,而不是静默归零 ——
否则"文件被写坏"和"全新安装"就无法区分,所有经验会一夜之间变回
"没被验证过"。

```bash
exp gc        # 会同时报出:重复的 id、读不出 frontmatter 的文件
```

`exp init` 是**幂等的修复命令**:`.exp/` 被误删、`lessons/` 被手滑删掉、
clone 下来缺目录 —— 再跑一次 `exp init` 就能补齐,**不会删任何已有经验**。

---

## 卸载

**数据不会被自动删除。**

```bash
exp status        # 先看清楚有什么、在哪
```

停用只需要去掉 hooks,数据留着:

- **项目层**建议保留 —— 它是要 commit 的团队资产,删掉等于回滚别人的经验
- **全局层**只影响你本机,想清就清

---

## 更多

| 文档 | 内容 |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | 设计取舍与原理:为什么计数器不进 git、为什么主题是算出来的、为什么零依赖 |
| [`packs/README.md`](plugins/exp/packs/README.md) | 怎么写一个经验包 |
| [`CHANGELOG.md`](CHANGELOG.md) | 变更记录 |

---

## 贡献

欢迎 issue 和 PR。跑测试:

```bash
python -m unittest discover -s tests -v      # 全部 45 个用例
python tests/run_without_pyyaml.py           # 真实屏蔽 PyYAML 再跑一遍
```

**提交经验包是最好的贡献方式** —— 你踩过的跨项目通用的坑,别人不用再踩。
标准见 [`packs/README.md`](plugins/exp/packs/README.md)。

---

## 许可

[MIT](LICENSE)
