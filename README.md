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
    索引1 注入3 复发2

── 检索漏检 (1) —— 触发词写偏了,该命中没命中 ──
  不要用 var 声明变量  (漏检3)
    现在写的触发词: 当你写 JavaScript 变量声明时

── 索引饿死 (12) —— 索引装不下,它们从没露过面 ──
  处置:扩容量、提优先级,或该上分层了 —— **不是**删经验。

处置建议:
  复发     → 重写 fix,让它可执行;或降级为 hypothesis
  漏检     → 重写 trigger,用你实际会想到的词
  投递饿死 → 查投递链路(会话身份 / 记账),**不要**改 trigger
  索引饿死 → 扩容量或提优先级,**不要**删经验
  死重     → 删掉,或合并进相近的经验
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

失败发生时,按**签名**(工具 + 错误码 + 归一化后的错误信息)归因。

判定要看**两个维度**:这条经验是怎么被匹配上的,以及它**有没有送到模型手上**。
两个维度交叉出四个格子,每个格子对应一种不同的毛病和不同的处方:

| 怎么匹配上的 | 看没看过做法 | 记为 | 说明什么 | 怎么修 |
|---|---|---|---|---|
| **签名**(精确) | ✅ 看过 | `recurred` | fix 不可执行 | 重写 **fix** |
| **签名**(精确) | ❌ 没送到 | `starved` | 投递链路坏了 | 查**投递**,**别动 trigger** |
| 语义(启发式) | ✅ 看过 | `recurred` | fix 不可执行 | 重写 **fix** |
| 语义(启发式) | ❌ 没送到 | `missed` | 触发词写偏了 | 重写 **trigger** |

**为什么必须分这两维** —— 上一版把签名命中也记成 `missed`,于是任何挂了签名的
经验,只要模型没主动读过正文,都会稳定进"检索漏检"桶,而 `gc` 会据此建议
**重写触发词**。那条触发词从头到尾没被查过,失败是靠签名对上的。
**误诊比不诊断更坏**:它让人去改一个完全没问题的字段。

> 签名是**结构性**断言(路径、数字、引号内容都归一化了),命中即"同一种失败",
> 岔子只可能出在投递端;语义匹配是**启发式**,会假阳性,才需要 `missed` 来暴露。
> 一句话:**结构性信号不产生 `missed`,启发式信号才产生。**

复发到阈值(`RECUR_THRESHOLD`,默认 2)且已确认的经验,
会被**自动降级**为 `needs_rewrite`。

**闭环不依赖任何人的自觉。** 失败时那条 fix 会直接送到模型眼前
(走 `exit 2` + stderr),**并且这次投递会被记账** —— 所以哪怕模型
从头到尾没主动跑过一次 `exp query`,`recurred` 也照样长得出来。

> 签名不是前提。没挂签名的经验靠失败信息本身的措辞比对(重合 ≥ 4),
> 精度低一些,但同样能判 `recurred` / `missed`。

---

## 装完之后自动发生什么

项目里跑过 `exp init` 之后,这四件事**不依赖任何人的自觉**:

| 时机 | Hook | 动作 |
|---|---|---|
| 会话开始 | `SessionStart` | 注入经验索引(标题 + 触发词),记 `indexed` |
| **你发出请求** | **`UserPromptSubmit`** | **按当前任务检索 → 把命中条目的做法送进上下文** |
| 工具失败 | `PostToolUseFailure` | 算签名 → **把做法送到模型眼前** → 归因 → 落原始事件 |
| 回合结束 | `Stop` | 把重复失败归拢成候选 |

**全部零 LLM 调用。** 归纳留给你自己 —— 你有当前会话的完整语境,
比任何外部调用都更懂当时发生了什么。

### 为什么第二条不是 `PreToolUse`

`PreToolUse` 看着更合适 —— 它知道**具体要跑什么命令**。但它的
`additionalContext` 是**和工具结果同一次**送达的:模型看到经验时,
那条命令已经跑完了。这不是实现选择,是 Messages API 的结构 ——
`tool_result` 必须紧跟 `tool_use`,**中间没有位置可以插**。
所以 `PreToolUse` 能改参数(`updatedInput`)、能拦(`permissionDecision`),
但**不能提前告知**。

`UserPromptSubmit` 在模型处理 prompt 之前触发,`additionalContext`
随 prompt 一起进上下文 —— 那才是"动手之前"。代价是它只看得到用户的
自然语言,看不到 `tool_input`。**看得见的意图,远不如到得及的时机重要。**

> 上面那一步是闭环的关键。上一版失败时也归因,但**送正文和记账是断开的**:
> 做法确实通过 stderr 送到了模型眼前,却没有记一笔注入 —— 于是模型真的
> 读过了,系统却当没送过,`recurred` 永远算不出来。
> **记了没送是漏报,送了没记是自欺。**

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
| `exp gc` | 体检:复发 / 漏检 / 投递饿死 / 索引饿死 / 死重 / 容量 |
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
exp list --health rotten         # 只看反复复发的
exp list --health missed         # 只看检索漏检的
exp list --health starved        # 只看投递饿死的
exp list --health starved_index  # 只看索引装不下的
exp list --layer global          # 只看全局层
exp list --by category           # 按主题分组(仅用于人工翻阅)
```

### 库比索引大的时候

**真正约束成本的是字符预算,不是条数。** 每条经验约 49 字符(标题 +
触发词两行),`INDEX_MAX_CHARS = 4000` 实测能装约 100 条。
`INDEX_MAX_ITEMS` 只是防爆上限(默认 120),正常情况下够不着。

超过之后,排在后面的经验不会常驻。`exp gc` 会在**超过的当天**就报出来:

```
── 容量告警 —— 库比索引大,必有经验排不进去 ──
  200 条经验,索引受限于 4000 字符预算,当前列出 100 条,排除 100 条。
  被排除的仍能被 `exp query` 检索到 —— 只是不常驻。
  但它们的 missed 永远算不出来(主动检索不会跑到它们),
  所以【库里没问题】是假象。
  处置:条数受限于字符预算就调高 INDEX_MAX_CHARS;
        确实需要更多条目就调 INDEX_MAX_ITEMS;
        或者把通用的经验挪到全局层、合并同类。
```

两条设计要点:

- **不等 30 天。** 死重需要时间(这条经验可能只是还没被用上),
  但"索引装不下"是**算术事实**,今天超了今天就成立。被挤掉这件事
  在渲染索引的那一刻当场记账(`excluded`),所以是事实而非推断。
- **告警报的是真正生效的那条约束。** 卡住的是字符预算就说字符预算,
  是条数就说条数 —— 报错的限制器会把人引向错误的处置。

而模型看到的那段文字是**可行动的**,不是给维护者看的告警:

```
> 这里只列出 100 条(超出 4000 字符预算);库里共 200 条。
> 没列出的用 `exp query "<你要做什么>"` 一样能检索到。
```

> **饿死的真正危害不是"没列出来",而是"模型不知道它存在"。**
> 说清楚这一点,截断就从静默失效变成了已知的不完整。

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
- **归因依赖会话身份。** 注入记账和失败归因发生在两个独立的进程里,
  靠 `session_id` 对上号。拿不到会话 ID 时(比如你手工在终端里跑 hook,
  又没有 payload),系统**不会猜** —— 它会跳过 recurrence 判定,
  只做骨架匹配。宁可少一个信号,也不拿错的 id 污染计数。
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

**计数器出现新字段是正常的。** `indexed`(进过索引几次)和 `starved`
(签名命中了却没送到)是后加的,老库里没有——缺失一律按 0 处理,
不需要跑 `exp migrate`。它们和 `injected` 一样只存在本机 `stats.json`,
不进版本库。

> 为什么值得单独加这两个:加之前,「进过索引但没人读」和
> 「连索引都没进过」在数据上**长得一模一样**(都是注入 0 次),
> 而处方正好相反 —— 前者该删,后者该扩容量。分不开就只能误诊。

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
