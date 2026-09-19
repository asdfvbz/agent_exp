#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp — 两层经验库(全局 + 项目级)。

存在的理由:同一个错误犯第二次是浪费。

但"记住"不等于"会想起来"。所以全部设计围绕两件事:

  1. 保证经验在【做事之前】被看到  —— 检索层
  2. 保证坑在【踩的那一瞬间】被记下 —— 捕获层

第 2 点是关键:没人会在犯错的当下主动记一条经验。
所以捕获必须由机器强制(hook),不能依赖自觉。

── 两层 ──────────────────────────────────────────
  <项目>/.exp/    项目级:跟着 repo 走,可 commit,团队共享
  ~/.exp/         全局级:跟着人走,跨项目复利

检索时两层合并,全局降权 —— 项目级更当用,全局级更泛。

── 捕获与蒸馏分离 ─────────────────────────────────
  hook(无 LLM)  → raw/*.jsonl    原始事件,必定发生
  exp distill    → 候选经验      批量归纳

原始事件本身是资产:它是评测集的原料,也是"到底踩了多少坑"的证据。
distill 不调用 LLM —— 它把候选整理出来,交给 agent 自己归纳
(宿主里那个模型比任何外部调用都更懂当前语境)。

── 闭环:注入记账 + 复发归因 ──────────────────────
  每次注入写 injections.jsonl。失败发生时按【签名】归因:

    recurred  注入了,还是犯了  → 负信号(经验写错了 / fix 太泛)
    missed    该注入,没注入    → 检索漏了(trigger 写偏了)

  两个信号都是机器算的,不需要人工标注。
  这是唯一能自动分辨"哪条经验是坏的"的机制。

── 零依赖 ────────────────────────────────────────
  经验库在会话启动路径上。它挂掉会拖垮整个会话。
  所以:纯标准库,文件是 Markdown + YAML frontmatter,git 友好。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECT_DIRNAME = ".exp"
GLOBAL_DIR = Path.home() / PROJECT_DIRNAME

# 经验文件的格式版本。
#
# **必须写进每个文件。** 经验是【用户的数据资产】——
# 插件升级时如果 frontmatter 格式变了,没有版本号就只能靠猜,
# 而猜错 = 弄坏用户数据。这是开源项目最不可原谅的错误。
#
# 缺失 = 0(本字段出现之前的文件),迁移时按 0 处理。
SCHEMA_VERSION = 1

CATEGORIES = ["基架", "度量", "流程", "写作", "市场", "其他"]
STATUSES = ["confirmed", "hypothesis", "refuted", "needs_rewrite"]
SEVERITIES = ["high", "medium", "low"]

# 自动降级 / 死重判定阈值
RECUR_THRESHOLD = 2      # 注入后仍复发 N 次 → 标记 needs_rewrite
STARVE_THRESHOLD = 2     # 骨架命中却送不到 N 次 → 索引饿死,多半是容量问题
DEAD_WEIGHT_DAYS = 30    # 创建 N 天、正文一次没投递过 → 死重 / 索引饿死

# 索引容量。超过它,平铺 list 就开始漏经验 ——
# 而漏掉的经验永远不会被注入,missed 也永远算不出来。
#
# **真正约束成本的是字符预算,不是条数。** 实测每条经验约 49 字符
# (标题 + trigger 两行),4000 字符能装约 78 条 —— 而
# INDEX_MAX_ITEMS 曾是 40,把一半预算白白浪费了。
# 现在条数只作为防爆上限(防止极端短的条目把条数撑爆),
# 有效容量由字符预算决定。
INDEX_MAX_ITEMS = 120
INDEX_MAX_CHARS = 4000

# 注入记账的会话窗口(分钟)。失败发生在这段时间内的注入才算"当时在上下文里"。
INJECT_WINDOW_MIN = 90

# ── 归因模式 ───────────────────────────────────────
#
# 归因要判两件事:这条经验**该不该**在这次失败里出现,以及它**有没有**出现。
# 前者靠检索,后者靠 injections.jsonl 记账。
#
# 检索有两档,精度差很多,必须分开对待:
#
#   skeleton  经验挂了签名,失败签名精确命中(路径/数字/引号内容都已归一化)。
#             这是**结构性**断言:系统确知"这是同一种失败"。所以它身上
#             出的岔子只可能是投递问题 → 记 starved,绝不记 missed。
#
#   related   没挂签名,只能用失败信息做二元组检索。这是**启发式**,
#             会假阳性 —— 所以它才需要 missed 这个信号来暴露 trigger 写偏。
#
# 一句话:**结构性信号不产生 missed,启发式信号才产生。**
#
# 踩过的坑:上一版两条路径都记 missed,于是任何挂了签名的经验,
# 只要模型没主动读过正文,都会稳定进 missed 桶 —— 而 gc 会据此
# 建议"重写 trigger"。那条 trigger 从头到尾没被查过,失败是靠签名
# 对上的。误诊比不诊断更坏(见 DESIGN.md「归因写错比不写更坏」)。
ATTRIBUTION_MODE = "related"

# ── 重复失败提示 ───────────────────────────────────
#
# 同一个签名当天第几次出现时,如果库里没有对应经验,就告诉模型。
#
# **为什么是 2:** 第 1 次出现无从判断它会不会重复 —— 那时候说什么都是噪声。
# 第 2 次是"重复"被**确证**的那一刻,也正是签名长出来的那一刻。
# 卡在 3 就意味着第 3 次学费已经交掉了。
#
# 这条提示的触发判据是【纯结构性】的:同一签名出现 N 次。
# 它不依赖 trigger 的措辞,不需要经验库里先有任何东西 ——
# 冷启动期唯一能工作的信号。
REPEAT_HINT_THRESHOLD = 2

# ── 任务前投递(UserPromptSubmit)────────────────────
#
# 这是**唯一**能真正在动作之前把经验送进上下文的通道。
#
# **为什么不是 PreToolUse:** 它的 `additionalContext` 是【和工具结果
# 同一次】送达的 —— 文档原话是 "added to Claude's context alongside
# the tool result"。而且这不是实现偷懒,是协议决定的:Messages API
# 要求 `tool_result` 必须紧跟 `tool_use`,**没有**"模型决定调用"和
# "工具真的执行"之间的那个位置可插。所以 PreToolUse 能改参数
# (updatedInput)、能拦(permissionDecision),但**不能提前告知**。
#
# UserPromptSubmit 在模型处理 prompt 之前触发,additionalContext 随
# prompt 一起进上下文 —— 那才是"动手之前"。
#
# 代价是它只能看到【用户的自然语言】,看不到具体的 tool_input。
# 这个交换是值得的:看得见的意图远不如到得及的时机重要。
# **每条 prompt 只推一条。** 实测(8 条 seed 库,4 条真实风格的 prompt):
# 一次命中 3 条的 prompt,那条真正相关的经验只排第 2 —— 推两条就是
# 一条相关 + 一条不相关,而不相关的那条会一直留在会话历史里。
# 推一条:错了只错一条,而且下一次 prompt 会推别的。
PRE_ACTION_MAX_LESSONS = 1
PRE_ACTION_MAX_CHARS = 1600     # 单条正文的预算
# 重合度阈值。**这个数字是量出来的,不是拍的。**
#
# 用真实会写的 trigger 和真实风格的 prompt 测(6 正例 / 3 反例):
#
#     阈值 1:  命中 5/6   误报 1/3
#     阈值 2:  命中 0/6   误报 0/3     ← 功能等于关闭
#     阈值 3:  命中 0/6   误报 0/3
#
# **两类在 1 分处完全重叠**:A 类分布 {0,1,1,1,1,1},B 类 {0,0,1}。
# 也就是说二元组分数几乎只区分"同不同话题",不区分"相关不相关"。
#
# 那为什么还是取 1:因为**真正在做区分的是排序,不是阈值**。
# 明显无关的 prompt("解释一下这个函数""总结项目架构")拿到 0,被挡住;
# 进来的都是话题相邻的,再由 rank 选出最像的那一条。
# 取 2 会让整个功能永不触发 —— 那是死代码,比有点噪声更坏。
#
# 已知弱点:匹配不到同义表达(见 DESIGN.md「为什么不用向量库」)。
# 反例样本只有 3 条,精度数字不可靠;`helped` / `recurred` 是它在
# 真实使用中暴露自己的方式。
PRE_ACTION_MIN_OVERLAP = 1


# ── 控制台编码 ─────────────────────────────────────
# 中文 Windows 控制台默认 GBK,输出非 GBK 字符会直接抛
# UnicodeEncodeError 把命令打断。统一走 UTF-8,失败就算了。
def _fix_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_fix_console()


def warn(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    warn(msg)
    sys.exit(code)


# ── YAML 子集 ──────────────────────────────────────
# 优先 PyYAML;没有就用内置的极简实现。
# 我们【只写自己定义的模式】,所以子集解析是安全的 ——
# 它不需要理解任意 YAML,只需要理解我们写出去的东西。
try:
    import yaml as _pyyaml
except Exception:
    _pyyaml = None


def _load_yaml(text: str) -> Dict[str, Any]:
    if _pyyaml is not None:
        try:
            d = _pyyaml.safe_load(text)
            return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return _mini_load(text)


def _dump_yaml(data: Dict[str, Any]) -> str:
    if _pyyaml is not None:
        try:
            return _pyyaml.safe_dump(
                data, allow_unicode=True, sort_keys=False, default_flow_style=False
            )
        except Exception:
            pass
    return _mini_dump(data)


def _mini_load(text: str) -> Dict[str, Any]:
    """极简 YAML 子集:key: scalar / key: [] / 两级缩进 dict。

    不支持嵌套列表、锚点、多行块 —— 我们的模式里也没有。
    """
    out: Dict[str, Any] = {}
    cur_key: Optional[str] = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if indent > 0 and cur_key:
            # 二级:挂到上一个 key 下
            sub = out.get(cur_key)
            if not isinstance(sub, dict):
                sub = {}
                out[cur_key] = sub
            sub[k] = _mini_scalar(v)
        else:
            if v == "":
                out[k] = {}
                cur_key = k
            else:
                out[k] = _mini_scalar(v)
                cur_key = k
    return out


def _mini_unquote(s: str) -> str:
    """剥掉外层引号并**还原转义**。

    `_mini_repr` 写出去时把 `"` 转义成 `\\"`;读回来必须转回去。
    只 strip 引号不还原转义,会让 `有"引号"` 变成 `有\\"引号\\"` ——
    **值被静默改坏**,而且只在没装 PyYAML 的机器上发生。
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


def _mini_scalar(v: str) -> Any:
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_mini_unquote(x) for x in inner.split(",") if x.strip()]
    # `""` 是空字符串,不是空值 —— 必须在下面的空值判断之前拦下
    if v == '""' or v == "''":
        return ""
    if v.lower() in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return _mini_unquote(v)


def _mini_dump(data: Dict[str, Any]) -> str:
    lines: List[str] = []
    for k, v in data.items():
        if isinstance(v, dict):
            lines.append(f"{k}:")
            for k2, v2 in v.items():
                lines.append(f"  {k2}: {_mini_repr(v2)}")
        else:
            lines.append(f"{k}: {_mini_repr(v)}")
    return "\n".join(lines) + "\n"


def _mini_repr(v: Any) -> str:
    if v is None:
        return "~"          # 显式空值,和空字符串区分开(见 _mini_scalar)
    if isinstance(v, list):
        return "[" + ", ".join(_mini_repr(x) for x in v) + "]"
    s = str(v)
    # 空字符串必须加引号 —— 否则读回来变成 "空",而空字符串和空值
    # 是两个不同的东西(`stats.last_injected: ""` vs `not: ~`)
    if s == "":
        return '""'
    # 冒号、引号、井号会破坏子集解析 —— 加引号兜住
    if any(c in s for c in ':#"\'') or s.strip() != s:
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


# ── 失败签名 ───────────────────────────────────────
# 按【结构】而非原文算签名 —— 否则换个措辞就当成新坑,
# 复发检测立刻失效。归一化掉:数字、路径、引号内容里的变量部分。
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\[^\s\"']+|/[^\s\"']{2,})")
_QUOTE_RE = re.compile(r"\"[^\"]{0,200}\"|'[^']{0,200}'")


def signature(tool: str, error: str, code: Any = "") -> str:
    """失败签名。

    **在源头就清代理项** —— 签名会被写文件、被打印、被比较。
    指望每个调用点都记得 scrub 是不可靠的,所以在这里兜死。
    """
    s = _scrub(str(error or ""))[:400]
    s = _PATH_RE.sub("<PATH>", s)
    s = _QUOTE_RE.sub("<STR>", s)
    s = re.sub(r"\b\d+\b", "<N>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _scrub(f"{tool}|{code}|{s[:140]}")


# ── 数据 ───────────────────────────────────────────
@dataclass
class Lesson:
    id: str
    path: Optional[Path] = None
    layer: str = "project"           # project | global
    schema: int = SCHEMA_VERSION     # 文件格式版本,用于迁移
    title: str = ""
    category: str = "其他"
    status: str = "hypothesis"
    severity: str = ""
    trigger: str = ""
    related: List[str] = field(default_factory=list)
    signatures: List[str] = field(default_factory=list)
    created: str = ""
    # ── 机器维护 ──
    injected: int = 0                # 【正文】投递过几次(模型看到了根因+做法)
    indexed: int = 0                 # 【标题】进过几次索引(session-start)
    excluded: int = 0                # 索引渲染过几次、而它没挤进去
    recurred: int = 0                # 看过做法还犯 → fix 不可执行
    missed: int = 0                  # 语义匹配该命中却没命中 → trigger 写偏
    starved: int = 0                 # 骨架精确命中却从没投递 → 投递策略的问题
    helped: int = 0                  # 显式正反馈
    last_injected: str = ""
    last_recurred: str = ""
    # ── 正文段落 ──
    symptom: str = ""
    root_cause: str = ""
    fix: str = ""
    evidence: str = ""
    scope: str = ""
    body_extra: str = ""

    @property
    def is_rotten(self) -> bool:
        """坏经验:注入了还是反复犯。"""
        return self.recurred >= RECUR_THRESHOLD

    @property
    def _aged(self) -> bool:
        if not self.created:
            return False
        try:
            return (dt.date.today()
                    - dt.date.fromisoformat(self.created)).days >= DEAD_WEIGHT_DAYS
        except Exception:
            return False

    @property
    def is_starved_index(self) -> bool:
        """索引饿死:索引渲染过、而它**一次都没挤进去**,也从没被投递。

        这不是经验本身有问题 —— 是索引装不下,它连露面的机会都没有。

        **为什么必须和死重分开:** 两者的现象完全一样(注入 0 次),
        但病因和处方相反。死重的处方是"删掉或重写 trigger";
        索引饿死的处方是"扩容量 / 上分层"。当成死重处理,会让用户
        去删一条本来很有用、只是没排上队的经验。

        **判据是 `excluded`(被挤掉的次数),不是年龄。**
        上一版这里挂了 `_aged`(30 天),是错的 ——
        死重确实需要时间(这条经验可能只是还没被用上),
        但**容量溢出是算术事实**:50 条经验、上限 40 条,
        今天就结构性地排除了 10 条,跟它放了多久毫无关系。

        拿年龄当判据的后果是:信号要 30 天后才出现,
        而这期间用户看到的是一切正常 —— 跟 missed 的失效模式一模一样。
        """
        return self.excluded > 0 and self.indexed == 0 and self.injected == 0

    @property
    def is_dead_weight(self) -> bool:
        """死重:进过索引(有机会被想起),却始终没被拉过全文。"""
        return self._aged and self.indexed > 0 and self.injected == 0

    def health(self) -> str:
        if self.status == "needs_rewrite" or self.is_rotten:
            return "rotten"
        if self.is_starved_index:
            return "starved_index"
        if self.is_dead_weight:
            return "dead"
        if self.starved >= STARVE_THRESHOLD:
            return "starved"
        if self.missed >= RECUR_THRESHOLD:
            return "missed"
        return "ok"


_SECTIONS = [
    ("表现", "symptom"),
    ("根因", "root_cause"),
    ("做法", "fix"),
    ("证据", "evidence"),
    ("适用范围", "scope"),
]


def _parse_body(text: str) -> Dict[str, str]:
    """把正文按 `## 标题` 切成段落。"""
    out: Dict[str, str] = {}
    cur: Optional[str] = None
    buf: List[str] = []
    for line in text.splitlines():
        m = re.match(r"^#{2,3}\s*(.+?)\s*$", line)
        if m:
            if cur:
                out[cur] = "\n".join(buf).strip()
            cur = m.group(1)
            buf = []
        elif cur:
            buf.append(line)
    if cur:
        out[cur] = "\n".join(buf).strip()
    return out


def _render_body(l: Lesson) -> str:
    parts = []
    for heading, attr in _SECTIONS:
        val = getattr(l, attr, "")
        if val:
            parts.append(f"## {heading}\n\n{val}\n")
    if l.body_extra:
        parts.append(l.body_extra.strip() + "\n")
    return "\n".join(parts)


# ── 一层存储 ───────────────────────────────────────
class Layer:
    """一个经验层(全局 或 项目级)。目录即集合,一条经验一个文件。"""

    # 进程级缓存:同一进程内多次 all() 只解析一次。
    # 关键场景是 `exp serve` —— HTTP 服务每 30 秒轮询 /api/data,
    # 没有它每次都要重解析整个库。磁盘缓存解决了跨进程,
    # 这个解决了同进程内的重复调用。
    _mem: Dict[str, Tuple[float, Dict[str, Lesson]]] = {}

    def __init__(self, root: Path, name: str):
        self.root = Path(root)
        self.name = name             # "global" | "project"
        self.lessons_dir = self.root / "lessons"
        self.raw_dir = self.root / "raw"

    # 静态布局对照,供 init 使用
    @property
    def exists(self) -> bool:
        return self.root.is_dir()

    def ensure(self) -> None:
        self.lessons_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def paths(self) -> List[Path]:
        if not self.lessons_dir.is_dir():
            return []
        return [
            p for p in sorted(self.lessons_dir.glob("*.md"))
            if not p.name.startswith("_")
        ]

    def load(self, p: Path) -> Lesson:
        text = p.read_text(encoding="utf-8", errors="replace")
        fm, body = _split_frontmatter(text)
        d = _load_yaml(fm)
        stats = self.load_stats().get(p.stem, {})
        secs = _parse_body(body)
        known = {h for h, _ in _SECTIONS}
        return Lesson(
            id=str(d.get("id") or p.stem),
            path=p,
            layer=self.name,
            schema=int(d.get("schema") or 0),
            title=str(d.get("title") or p.stem),
            category=str(d.get("category") or "其他"),
            status=str(d.get("status") or "hypothesis"),
            severity=str(d.get("severity") or ""),
            trigger=str(d.get("trigger") or "").strip(),
            related=list(d.get("related") or []),
            signatures=list(d.get("signatures") or []),
            created=str(d.get("created") or ""),
            injected=int(stats.get("injected") or 0),
            indexed=int(stats.get("indexed") or 0),
            excluded=int(stats.get("excluded") or 0),
            recurred=int(stats.get("recurred") or 0),
            missed=int(stats.get("missed") or 0),
            starved=int(stats.get("starved") or 0),
            helped=int(stats.get("helped") or 0),
            last_injected=str(stats.get("last_injected") or ""),
            last_recurred=str(stats.get("last_recurred") or ""),
            symptom=secs.get("表现", ""),
            root_cause=secs.get("根因", ""),
            fix=secs.get("做法", ""),
            evidence=secs.get("证据", ""),
            scope=secs.get("适用范围", ""),
            body_extra="\n".join(
                f"## {k}\n\n{v}" for k, v in secs.items() if k not in known
            ),
        )

    # ── 解析缓存 ───────────────────────────────────
    #
    # 每次 hook 全量重解析是 O(n) 次 YAML + 正则,实测 199 条要 ~104ms,
    # 而 post-failure 挂在【每次工具失败】上 —— 一轮密集调试能触发几十次。
    #
    # 缓存按 (mtime_ns, size) 失效,所以手改文件、git pull 都会自动重算。
    # **缓存不覆盖 stats** —— 计数走 stats.json,两者独立失效。
    @property
    def _cache_path(self) -> Path:
        return self.root / "index.json"

    def _file_sig(self, p: Path) -> str:
        try:
            st = p.stat()
            return f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            return ""

    def _load_cache(self) -> Dict[str, Any]:
        try:
            d = json.loads(self._cache_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) and d.get("v") == SCHEMA_VERSION else {}
        except Exception:
            return {}

    def all(self) -> List[Lesson]:
        paths = self.paths()
        # 目录签名的廉价探测:文件数 + 总 mtime。变了才往下走。
        try:
            sig = hash(tuple((p.name, self._file_sig(p)) for p in paths))
        except Exception:
            sig = 0.0
        ck = str(self.root)
        memo = Layer._mem.get(ck)
        if memo and memo[0] == sig:
            return list(memo[1].values())

        cache = self._load_cache()
        entries: Dict[str, Any] = cache.get("files") or {}

        fresh: Dict[str, Any] = {}
        parsed: List[Dict[str, Any]] = []
        dirty = False

        for p in paths:
            sig = self._file_sig(p)
            hit = entries.get(p.name)
            if hit and hit.get("sig") == sig and "data" in hit:
                parsed.append(hit["data"])
                fresh[p.name] = hit
            else:
                d = self._parse_file(p)
                parsed.append(d)
                fresh[p.name] = {"sig": sig, "data": d}
                dirty = True

        # 文件被删 → 缓存也得跟着缩
        if set(fresh) != set(entries):
            dirty = True

        if dirty:
            try:
                self._write_cache({"v": SCHEMA_VERSION, "files": fresh})
            except Exception:
                pass       # 缓存写不进去不该影响读

        stats = self.load_stats()
        out: List[Lesson] = []
        for d, p in zip(parsed, paths):
            l = self._from_dict(d, stats.get(d["id"], {}))
            l.path = p          # 回填路径 —— 缓存里只存解析结果,不存路径
            out.append(l)
        Layer._mem[ck] = (sig, {l.id: l for l in out})
        return out

    @classmethod
    def clear_memo(cls) -> None:
        """计数变更后必须清 —— 否则同进程读到的还是旧计数。"""
        cls._mem.clear()

    def _write_cache(self, payload: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._cache_path)

    def _parse_file(self, p: Path) -> Dict[str, Any]:
        """解析一个经验文件,**不含 stats**。结果可缓存。"""
        text = p.read_text(encoding="utf-8", errors="replace")
        fm, body = _split_frontmatter(text)
        d = _load_yaml(fm)
        secs = _parse_body(body)
        known = {h for h, _ in _SECTIONS}
        return {
            "id": str(d.get("id") or p.stem),
            "schema": int(d.get("schema") or 0),
            "title": str(d.get("title") or p.stem),
            "category": str(d.get("category") or "其他"),
            "status": str(d.get("status") or "hypothesis"),
            "severity": str(d.get("severity") or ""),
            "trigger": str(d.get("trigger") or "").strip(),
            "related": [str(x) for x in (d.get("related") or [])],
            "signatures": [str(x) for x in (d.get("signatures") or [])],
            "created": str(d.get("created") or ""),
            "symptom": secs.get("表现", ""),
            "root_cause": secs.get("根因", ""),
            "fix": secs.get("做法", ""),
            "evidence": secs.get("证据", ""),
            "scope": secs.get("适用范围", ""),
            "body_extra": "\n".join(
                f"## {k}\n\n{v}" for k, v in secs.items() if k not in known),
        }

    def _from_dict(self, d: Dict[str, Any], stats: Dict[str, Any]) -> Lesson:
        return Lesson(
            id=d["id"], layer=self.name, schema=int(d.get("schema") or 0),
            title=d["title"], category=d["category"], status=d["status"],
            severity=d["severity"], trigger=d["trigger"],
            related=list(d.get("related") or []),
            signatures=list(d.get("signatures") or []),
            created=d.get("created", ""),
            injected=int(stats.get("injected") or 0),
            indexed=int(stats.get("indexed") or 0),
            excluded=int(stats.get("excluded") or 0),
            recurred=int(stats.get("recurred") or 0),
            missed=int(stats.get("missed") or 0),
            starved=int(stats.get("starved") or 0),
            helped=int(stats.get("helped") or 0),
            last_injected=str(stats.get("last_injected") or ""),
            last_recurred=str(stats.get("last_recurred") or ""),
            symptom=d.get("symptom", ""), root_cause=d.get("root_cause", ""),
            fix=d.get("fix", ""), evidence=d.get("evidence", ""),
            scope=d.get("scope", ""), body_extra=d.get("body_extra", ""),
        )

    def get(self, lesson_id: str) -> Optional[Lesson]:
        p = self.lessons_dir / f"{lesson_id}.md"
        if p.exists():
            return self.load(p)
        hits = [l for l in self.all() if lesson_id in l.id or lesson_id in l.title]
        return hits[0] if len(hits) == 1 else None

    def duplicate_ids(self) -> Dict[str, List[str]]:
        """找出 id 重复的经验。

        **id 重复会让"按 id 取经验"彻底失效** —— 而且失败方式很隐蔽:
        `get()` 找不到(因为有两条,歧义),但 `list` 却显示得好好的。
        更糟的是归因会按 id 记账,两条会争抢同一份计数。

        只在 id 相同时才算重复 —— 文件名不同但 id 相同是真问题;
        文件名相同是不可能的(同一目录)。
        """
        seen: Dict[str, List[str]] = {}
        for p in self.paths():
            try:
                fm, _ = _split_frontmatter(
                    p.read_text(encoding="utf-8", errors="replace"))
                d = _load_yaml(fm)
                lid = str(d.get("id") or p.stem)
            except Exception:
                continue
            seen.setdefault(lid, []).append(p.name)
        return {k: v for k, v in seen.items() if len(v) > 1}

    def save(self, l: Lesson) -> None:
        """写盘。**写之前先序列化并回读校验** —— 一个字符的错误
        会让所有读该文件的工具同时报错,排查时看到的是不相关的报错。

        这条是踩过坑的:虚构字符 / markdown 星号进 YAML 会静默毁掉解析。
        """
        self.ensure()
        fm = {
            "schema": SCHEMA_VERSION,      # 版本号必须在每个文件里
            "id": l.id,
            "title": l.title,
            "category": l.category,
            "status": l.status,
            "severity": l.severity,
            "trigger": l.trigger,
            "related": l.related,
            "signatures": l.signatures,
            "created": l.created,
            # 计数器不在这里 —— 见下面 load_stats 的说明
        }
        text = "---\n" + _dump_yaml(fm) + "---\n\n" + _render_body(l) + "\n"

        # 回读校验:解析不出来就不许落盘
        fm2, _ = _split_frontmatter(text)
        back = _load_yaml(fm2)
        if str(back.get("id") or "") != l.id:
            raise ValueError(
                f"经验序列化后回读失败,拒绝写入: {l.id}\n"
                f"  多半是 trigger/title 里有破坏 YAML 的字符(星号、英文引号)。"
            )

        p = self.lessons_dir / f"{_slug(l.id)}.md"
        p.write_text(text, encoding="utf-8")
        l.path = p

    # ── 计数器(本地遥测) ──────────────────────────
    #
    # **计数器不写进 .md。** 这个决定是有意的:
    #
    # 经验文件要 commit,团队共享。而注入/复发是高频写的机器遥测 ——
    # 写进被 commit 的文件意味着每天几十次冲突,而且冲突发生在
    # 人类写的正文旁边,有损坏内容的风险。
    #
    # 所以:经验正文(+ 人决定的 status)= commit;
    #       计数器 = 本机、gitignore、按需重算。
    # 副作用是计数是本机的,不是全队的 —— 但这本来就该是本机信号。
    @property
    def _stats_path(self) -> Path:
        return self.root / "stats.json"

    def load_stats(self) -> Dict[str, Dict[str, Any]]:
        """读计数。

        **解析失败时先试备份,而不是直接返回空。**

        直接回空会让"文件被写坏"和"全新安装"变得无法区分 ——
        所有计数静默归零,而界面上看不出任何异常。计数是这个系统
        唯一的信号来源,静默归零等于把**所有**经验重新变回"没被验证过"。

        所以:_write_stats 每次都留一份 .bak,读失败时回退到它。
        """
        for path in (self._stats_path, self._stats_path.with_suffix(".json.bak")):
            try:
                txt = path.read_text(encoding="utf-8")
            except Exception:
                continue
            try:
                d = json.loads(txt)
            except Exception:
                continue          # 坏了就试下一份
            if isinstance(d, dict):
                return d
        return {}

    def _write_stats(self, d: Dict[str, Dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._stats_path.with_suffix(".json.tmp")
        blob = json.dumps(d, ensure_ascii=False, indent=1)
        try:
            tmp.write_text(blob, encoding="utf-8")
            tmp.replace(self._stats_path)  # 原子替换,别留半个文件
        except Exception:
            return
        # 上一份留作备份 —— 覆盖前先确定当前内容是好的
        try:
            bak = self._stats_path.with_suffix(".json.bak")
            bak.write_text(blob, encoding="utf-8")
        except Exception:
            pass

    def bump_many(self, entries: Iterable[Tuple[str, str, int]]) -> None:
        """批量加计数,**一次读 + 一次写**。

        单条 bump 每次都要 load + write 整个 stats.json ——
        session-start 要给 N 条经验各记一次注入,那就是 N 次全文件读写。
        200 条经验 = 400 次文件操作,还开 200 个原子替换窗口。

        entries 是 (lesson_id, key, n) 三元组 —— **n 必须逐条带**,
        不能共用一个默认值:那会让"加 5 次"静默变成"加 1 次"。
        """
        entries = list(entries)
        if not entries:
            return
        # **读-改-写必须在锁里。** 两个会话同时跑时,没锁的
        # "读 → 改 → 原子替换"会让后写的覆盖先写的,计数静默丢失 ——
        # 而计数是这个系统唯一的信号来源,丢了就查不出坏经验。
        with self._lock():
            d = self.load_stats()
            ts = _now()
            for lesson_id, key, n in entries:
                e = d.setdefault(lesson_id, {})
                e[key] = int(e.get(key) or 0) + int(n)
                if key in ("injected", "indexed", "recurred"):
                    e["last_" + key] = ts
            self._write_stats(d)
        # 计数变了(文件签名没变),进程缓存必须失效
        Layer.clear_memo()

    @contextmanager
    def _lock(self, timeout: float = 120.0):
        """跨进程排他锁。

        用 lock 文件而不是 flock —— Windows 上没有 flock,
        而"独占创建"是可移植的原子操作。

        **两个 Windows 特有的坑,都是实测丢数据之后才发现的:**

        1. 任何获取失败都必须重试,**不能放弃**。删文件被占用时
           Windows 抛的是 PermissionError 而不是 FileExistsError;
           上一版把它归进"建不了锁"直接放行,于是那些进程
           **无锁写入**,20 个并发丢了 2 次。宁可等到超时。

        2. 创建后**立刻关闭 fd**。拿着句柄去 unlink 在 Windows 上
           会失败(文件被占用),锁永远删不掉 —— 那会变成死锁,
           而加锁的死锁比不加锁的丢数据更糟。

        3. 超时必须**远大于**临界区耗时。临界区只有几毫秒读写,
           但 40 个进程同时启动时,光是创建文件本身就要排队。
           实测 timeout=10s 时 40 并发丢 10 次 —— 因为超时后
           上一版会静默放弃,而现在会一直等到拿到为止。

        **宁可等待,不可静默丢弃。** 计数是这个系统唯一的信号来源,
        "少记几次"和"没记"在诊断上是同一件事:你查不出哪条经验是坏的。
        """
        lock_path = self.root / ".stats.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        got = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                fd = os.open(str(lock_path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)              # 见坑 2
                got = True
                break
            except OSError:
                # 见坑 1:FileExistsError 和 PermissionError 都重试。
                # 顺带清理陈旧锁(持锁进程崩了会留下它)
                try:
                    if time.time() - lock_path.stat().st_mtime > 15:
                        os.unlink(str(lock_path))
                        continue
                except OSError:
                    pass
                time.sleep(0.02)
        try:
            yield
        finally:
            if got:
                try:
                    os.unlink(str(lock_path))
                except OSError:
                    pass

    def reset_stats(self) -> None:
        self._write_stats({})
        Layer.clear_memo()

    # ── 原始事件 ──
    def append_raw(self, record: Dict[str, Any]) -> None:
        """追加一条原始事件。

        **绝不能抛异常。** 这条路径挂在每次工具失败上,它崩了
        等于整个归因静默失效 —— 而表现只是"坏经验怎么一直没被标出来"。

        所以:序列化前清代理项,写盘也包在 try 里。
        遥测写不进去不该影响用户的主流程。
        """
        self.ensure()
        day = dt.date.today().isoformat()
        try:
            line = json.dumps(record, ensure_ascii=False)
        except Exception:
            try:
                line = json.dumps(
                    {k: _scrub(str(v)) for k, v in record.items()},
                    ensure_ascii=False)
            except Exception:
                return
        try:
            with (self.raw_dir / f"{day}.jsonl").open(
                    "a", encoding="utf-8") as f:
                f.write(_scrub(line) + "\n")
        except Exception:
            pass

    def raw_events(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """读原始事件。

        `since` 是 ISO 日期前缀(如 "2026-09-19")—— **只要那一天起的文件**。
        Stop hook 必须用它:否则每次回合结束都要重解析全部历史,
        半年后 raw/ 里几千条,每次都白读。
        """
        if not self.raw_dir.is_dir():
            return []
        out = []
        for p in sorted(self.raw_dir.glob("*.jsonl")):
            if since and p.stem < since:      # 文件名就是日期,字典序即时间序
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out

    # ── 注入记账 ──
    @property
    def _inj_path(self) -> Path:
        return self.root / "injections.jsonl"

    def log_injection(self, ids: Iterable[str], context: str, session: str,
                      level: str = "content") -> None:
        """记一笔注入。

        `level` 区分两种注入,这个区分是归因正确性的前提:

          index    会话启动时注入的索引 —— 模型只看到【标题 + 触发词】。
                   它知道有这么条经验,但没看到 fix。
          content  query / show 的结果 —— 模型看到了【根因 + 做法】。

        混在一起记会让两个信号同时失效:全部算 content 则 `missed`
        永不触发(什么都"注入过");全部算 index 则 `recurred` 会在
        模型根本没读过 fix 的情况下误报。
        """
        ids = list(ids)
        if not ids:
            return
        self.ensure()
        rec = {
            "ts": _now(),
            "session": session,
            "level": level,
            "context": context[:200],
            "ids": ids,
        }
        try:
            with self._inj_path.open("a", encoding="utf-8") as f:
                f.write(_scrub(json.dumps(rec, ensure_ascii=False)) + "\n")
        except Exception:
            pass       # 遥测写不进去不该影响主流程

    def recent_injections(self, session: str = "",
                          window_min: int = INJECT_WINDOW_MIN,
                          level: Optional[str] = None) -> set:
        """最近被注入过的经验 id 集合。用于归因:失败发生时,
        哪些经验其实"当时就在上下文里"。

        level=None 表示两种都算。
        """
        if not self._inj_path.exists():
            return set()
        if not session:
            # 会话身份未知(见 session_id())。无法判断任何一条注入
            # "当时在不在上下文里" —— 返回空集比按最近时间瞎猜更诚实。
            # 归因会因此只走骨架路径,不做 recurrence 判定。
            return set()
        cutoff = dt.datetime.now() - dt.timedelta(minutes=window_min)
        out: set = set()
        for line in self._inj_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if session and r.get("session") not in ("", session):
                continue
            if level and r.get("level", "content") != level:
                continue
            try:
                ts = dt.datetime.fromisoformat(r.get("ts", ""))
            except Exception:
                continue
            if ts >= cutoff:
                out.update(r.get("ids") or [])
        return out


def _split_frontmatter(text: str) -> Tuple[str, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[3:end], text[end + 4:]
    return "", text


def _slug(text: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "-", text.strip())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:60] or "untitled"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ── 检索 ───────────────────────────────────────────
def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# 高频虚词 —— 中文二元组会把它们切出来,而它们在【任何】句子里都出现,
# 于是贡献的是噪声不是相关度。实测:
#
#   "我要设一个超时阈值"   与 "当你要写一个数字阈值时"  → 2 分
#   "我要设一个重试次数阈值" 与 同一条                  → 也是 2 分
#
# 两次共有的都是「一个」+「阈值」,而"超时"和"重试次数"的差别被
# 虚词抹平了 —— **打分器在分辨两条相关经验时,靠的是噪声。**
# 这跟 min_overlap 那条原则是同一个问题:宁可漏报,不要给出误导性匹配。
#
# 只放【真正哪里都出现】的词。范围宁可小:误删一个实词是静默漏检
# (而 missed 计数就是用来暴露漏检的),留着虚词则是假阳性。
# 刻意**不**包含「一个」「可以」「我们」之外的任何两字组合 ——
# 「数据」「系统」这类词看着泛,但在某些库里有实义,删了会误伤。
_ZH_STOP = {
    "一个", "一些", "一种", "一次", "一下", "什么", "怎么", "如何",
    "这个", "那个", "这些", "那些", "这样", "那样", "这是",
    "可以", "需要", "应该", "必须", "可能", "已经", "还是", "或者",
    "因为", "所以", "但是", "如果", "然后", "就是", "不是", "没有",
    "我们", "你们", "他们", "自己", "现在", "时候", "问题", "情况",
    "进行", "通过", "对于", "关于", "以及", "而且", "并且", "并且",
}
_EN_STOP = {"the", "and", "for", "with", "that", "this", "you", "are",
            "not", "but", "can", "will", "when", "your", "have", "from"}


def _terms(text: str) -> set:
    """切成检索词。中文用二元组 —— 无需词典,零依赖,
    对「追读率」「风格锁」这类自定义术语够用。

    **虚词必须剔除。** 二元组把「一个」「可以」这类词也切了出来,
    它们在任何句子里都出现,于是"相关"和"都提到了同一批虚词"
    变成同一件事。见 _ZH_STOP 里的实测数据。
    """
    s = re.sub(r"[^\w一-鿿]+", " ", (text or "").lower())
    words: set = set()
    for chunk in s.split():
        if re.match(r"^[一-鿿]+$", chunk):
            for i in range(len(chunk) - 1):
                gram = chunk[i:i + 2]
                if gram not in _ZH_STOP:
                    words.add(gram)
            if len(chunk) == 1:
                words.add(chunk)
        elif len(chunk) > 1 and chunk not in _EN_STOP:
            words.add(chunk)
    return words


_SEV_W = {"high": 2, "medium": 1, "low": 0, "": 0}

# 健康度的文案只在【一处】定义,CLI 和网页共用 ——
# 两边各写一份必然漂移,而对"哪些经验是坏的"给出不同答案
# 是不可接受的:那是这个系统最核心的输出。
HEALTH_LABEL = {
    "ok": "正常",
    "missed": "检索漏检",
    "dead": "死重",
    "rotten": "反复复发",
    "starved": "投递饿死",
    "starved_index": "索引饿死",
}
# 严重度顺序 —— CLI 分组、网页分组共用，必须一致。
HEALTH_ORDER = ["rotten", "starved", "starved_index", "missed", "dead", "ok"]
# 分组标题。**后两类刻意写明"不是经验的错"**：它们的现象和 dead 一样
# (注入 0 次)，但处方相反 —— 当成死重去删，会删掉一批只是没排上队的经验。
HEALTH_HEAD = {
    "rotten":        "反复复发 —— 注入了还是犯,fix 不可执行",
    "missed":        "检索漏检 —— 触发词写偏了,该命中没命中",
    "starved":       "投递饿死 —— 签名命中了却没送到,不是 trigger 的问题",
    "starved_index": "索引饿死 —— 索引装不下,它从没露过面(别删)",
    "dead":          f"死重 —— 进过索引却 {DEAD_WEIGHT_DAYS} 天没被拉过全文",
    "ok":            "正常",
}
# **每一类的"怎么办"必须写清楚,尤其是 starved 那两条。**
# 前四类的处方都是"改经验本身";后两类恰恰相反 —— 经验没问题,
# 坏的是投递策略或容量。混为一谈会让人删掉/改坏本来有用的经验。
HEALTH_ACTION = {
    "rotten": "注入了还是反复犯 —— fix 多半不可执行,重写它。",
    "missed": "该命中却没命中 —— 触发词写偏了,用你实际会想到的词重写。",
    "dead": "进过索引却从没被拉过全文 —— 考虑删掉或合并进相近的经验。",
    "starved": ("签名精确命中过,做法却从没送到模型手上 —— "
                "**不是 trigger 的问题,别改它**;查投递链路。"),
    "starved_index": ("索引装不下,它从没露过面 —— "
                      "扩容量或提优先级,**不要删这条经验**。"),
    "ok": "",
}


# ── 两层经验库 ─────────────────────────────────────
class Library:
    """两层合并视图。

    检索时全局层【降权】而非排除 —— 项目级更当用,全局级更泛,
    但全局级里那些跨项目通用的坑(比如 YAML 星号)必须能浮上来。
    """

    GLOBAL_PENALTY = 2

    def __init__(self, exp_dir: Optional[Path] = None,
                 include_global: bool = True):
        """`exp_dir` 是 **`.exp/` 目录本身**,不是项目根目录。

        这个参数以前叫 `project_root`,而它收的却是 `.exp/` ——
        误导性命名会让人传错,而传错的后果是"机制在某些路径下静默不激活"。
        """
        self.layers: List[Layer] = []
        # 只有 .exp/ 【真的存在并不为空】才挂上项目层。
        # 无条件挂会让 enabled 在任何路径上都返回 True ——
        # 而 enabled 是"要不要激活整套机制"的总开关,
        # 误报为 True 意味着在无关项目里捕获噪声、注入无关经验。
        if exp_dir is not None and (Path(exp_dir) / "lessons").is_dir():
            self.layers.append(Layer(exp_dir, "project"))
        if include_global and GLOBAL_DIR.is_dir():
            self.layers.append(Layer(GLOBAL_DIR, "global"))

    @property
    def project_layer(self) -> Optional[Layer]:
        for l in self.layers:
            if l.name == "project":
                return l
        return None

    @property
    def global_layer(self) -> Optional[Layer]:
        for l in self.layers:
            if l.name == "global":
                return l
        return None

    @property
    def enabled(self) -> bool:
        """项目级【目录真的存在】才算激活。

        装插件是全局的,激活是按项目的 —— 否则它会在无关项目里
        捕获噪声、注入无关经验。

        检查的是 `lessons/` 目录而不只是 `.exp/` 存在 ——
        一个空的 `.exp/`(比如用户手动 mkdir 出来的)没有初始化过,
        不该激活任何东西。
        """
        l = self.project_layer
        return l is not None and l.lessons_dir.is_dir()

    def all(self) -> List[Lesson]:
        out: List[Lesson] = []
        for layer in self.layers:
            out.extend(layer.all())
        return out

    def get(self, lesson_id: str) -> Optional[Lesson]:
        for layer in self.layers:
            hit = layer.get(lesson_id)
            if hit:
                return hit
        return None

    def query(self, context: str, limit: int = 5,
              min_overlap: int = 2) -> List[Lesson]:
        """按上下文检索。

        min_overlap 是必要的:二元组很容易靠常用字产生假阳性。
        宁可漏报,也不要给出误导性匹配 —— 错误的经验比没有经验更坏。
        """
        if not context:
            return []
        ctx = _terms(context)
        if not ctx:
            return []

        scored: List[Tuple[int, Lesson]] = []
        for l in self.all():
            if l.status == "refuted":
                continue
            trig = _terms(l.trigger + " " + l.scope + " " + l.title)
            if not trig:
                continue
            overlap = ctx & trig
            if len(overlap) < min_overlap:
                continue
            score = len(overlap) * 2
            score += _SEV_W.get(l.severity, 0)
            if l.status == "confirmed":
                score += 1
            if l.layer == "global":
                score -= self.GLOBAL_PENALTY
            if l.is_rotten:
                score -= 3          # 坏经验降权,但不隐藏 —— 仍要能看见
            scored.append((score, l))

        scored.sort(key=lambda x: (-x[0], x[1].title))
        return [l for _, l in scored[:limit]]

    def by_signature(self, sig: str) -> List[Lesson]:
        """挂了签名且精确命中的经验。

        **这是库里唯一的确定性匹配。** 二元组检索是启发式的,会假阳性;
        签名是结构化的(路径、数字、引号内容都归一化了),命中即"同一种失败"。
        所以投递和归因都应该优先走它 —— 见 ATTRIBUTION_MODE 的说明。
        """
        if not sig:
            return []
        return [l for l in self.all() if sig in l.signatures]

    def relevant(self, context: str, limit: int = 2,
                 min_overlap: int = 4) -> List[Lesson]:
        """语义相关。归因专用:只认高重合,宁漏勿错。

        `min_overlap` 卡在 4(而非 query 默认的 2)是刻意的 ——
        归因写错比不写更坏,误判会让人去"修"一条本来没问题的经验。
        """
        return self.query(context, limit=limit, min_overlap=min_overlap)

    # ── 导出量:现算,不存 ──────────────────────────
    #
    # 判据:**凡是需要"其他"桶的归类,都是导出量,不该存。**
    #
    #   状态(status / layer)  —— 状态机、二分决定,不需要"其他" → 存
    #   主题(category)       —— 永远会有新主题,永远需要"其他" → 现算
    #
    # 存下来的主题一定会烂:新经验放不进 → 塞进最接近的桶 →
    # 那个桶变成垃圾桶 → 没人记得边界在哪。而"算出来的"不会烂,
    # 因为它每次都是根据当下的数据重算的 —— 和 health 一样。
    def _key(self, l: Lesson) -> set:
        """一条经验的检索特征。刻意【不含 category】——
        它是导出量,让它进匹配池会稀释真正有效的 trigger。
        """
        return _terms(l.trigger + " " + l.title)

    def neighbors(self, target: Lesson, k: int = 3,
                  min_sim: float = 0.22) -> List[Tuple[float, Lesson]]:
        """按 trigger 相似度找邻居。

        这就是 `related` 该有的形态 —— 之前它是个手工填的存储字段,
        结果 13/13 全空。**手工维护的结构一定维护不动。**
        """
        t = self._key(target)
        if not t:
            return []
        out: List[Tuple[float, Lesson]] = []
        for l in self.all():
            if l.id == target.id or l.status == "refuted":
                continue
            sim = _jaccard(t, self._key(l))
            if sim >= min_sim:
                out.append((sim, l))
        out.sort(key=lambda x: (-x[0], x[1].title))
        return out[:k]

    def clusters(self, min_sim: float = 0.28,
                 min_size: int = 2) -> List[List[Lesson]]:
        """按 trigger 相似度贪心聚类。**每次现算,不落盘。**

        为什么不存:聚类不稳定 —— 加一条经验,整个结构会移位;
        换个特征,分组就变了。**用弱信号做永久性结构决定,
        比不做决定更危险。**

        所以它只用于【给人看】,不用于给经验贴永久标签。
        """
        items = [l for l in self.all() if l.status != "refuted"]
        keys = {l.id: self._key(l) for l in items}
        pool = list(items)
        groups: List[List[Lesson]] = []
        while pool:
            seed = pool.pop(0)
            group = [seed]
            rest = []
            for l in pool:
                if _jaccard(keys[seed.id], keys[l.id]) >= min_sim:
                    group.append(l)
                else:
                    rest.append(l)
            pool = rest
            if len(group) >= min_size:
                groups.append(sorted(group, key=lambda x: x.title))
        groups.sort(key=lambda g: -len(g))
        return groups

    def merge_candidates(self, min_sim: float = 0.5) -> List[Tuple[float, Lesson, Lesson]]:
        """相似度高到该合并的经验对。

        这是"整理"真正需要的东西 —— `gc` 报出 8 条死重时,
        你需要知道的是"这四条其实是同一个原则",而不是一份平铺清单。
        """
        items = [l for l in self.all() if l.status != "refuted"]
        keys = {l.id: self._key(l) for l in items}
        out: List[Tuple[float, Lesson, Lesson]] = []
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                sim = _jaccard(keys[a.id], keys[b.id])
                if sim >= min_sim:
                    out.append((sim, a, b))
        out.sort(key=lambda x: -x[0])
        return out

    def attribute(self, l: Lesson, mode: str, seen_content: Optional[bool],
                  pending: List[Tuple[Lesson, str, int]]) -> str:
        """给一次失败归因。返回记下的信号名。

        **两个维度,四个格子** —— 把 mode 和 seen_content 分开之后,
        每个格子都是一个独立的、有明确处置的问题:

                         看过做法            没看过做法
          骨架命中   recurred  → 改 fix      starved  → 改投递
          语义命中   recurred  → 改 fix      missed   → 改 trigger

        重点是**右下角**:骨架命中但没送正文,绝不能再记 missed。
        签名撞上了是硬证据,这条 trigger 一个字都不需要改 ——
        该改的是"为什么它没被送达"。

        `seen_content` 为 None 表示会话身份未知,无从判断看没看过 ——
        此时只有骨架模式敢下"确定是它、却没送达"的结论,记 starved;
        语义模式退回 missed(它本来就是个弱信号,不必再帮它猜)。
        依据见 session_id()。
        """
        if seen_content is True:
            pending.append((l, "recurred", 1))
            return "recurred"
        if mode == "skeleton":
            pending.append((l, "starved", 1))
            return "starved"
        pending.append((l, "missed", 1))
        return "missed"

    def bump(self, l: Lesson, key: str, n: int = 1) -> None:
        """给一条经验加计数。同时更新内存对象和本机 stats.json。

        **绝不因此重写经验文件** —— 计数器不进 commit 的文件。

        批量场景请用 bump_all() —— 每条都单独写一次是 O(n) 次全文件 IO。
        """
        self.bump_all([(l, key, n)])

    def bump_all(self, entries: Iterable[Tuple[Lesson, str, int]]) -> None:
        """批量加计数。按层分组,**每层只写一次 stats.json**。"""
        by_layer: Dict[str, List[Tuple[str, str, int]]] = {}
        ts = _now()
        for l, key, n in entries:
            setattr(l, key, getattr(l, key, 0) + n)
            if key in ("injected", "indexed", "recurred"):
                setattr(l, "last_" + key, ts)
            # n 必须逐条带上 —— 见 bump_many 的说明
            by_layer.setdefault(l.layer, []).append((l.id, key, n))
        for layer in self.layers:
            if layer.name in by_layer:
                layer.bump_many(by_layer[layer.name])

    def stats(self) -> Dict[str, Any]:
        items = self.all()
        return {
            "total": len(items),
            "project": sum(1 for l in items if l.layer == "project"),
            "global": sum(1 for l in items if l.layer == "global"),
            "confirmed": sum(1 for l in items if l.status == "confirmed"),
            "hypothesis": sum(1 for l in items if l.status == "hypothesis"),
            "refuted": sum(1 for l in items if l.status == "refuted"),
            "rotten": sum(1 for l in items if l.health() == "rotten"),
            "dead": sum(1 for l in items if l.health() == "dead"),
            "missed": sum(1 for l in items if l.health() == "missed"),
            "starved": sum(1 for l in items if l.health() == "starved"),
            "starved_index": sum(
                1 for l in items if l.health() == "starved_index"),
            "indexed": sum(1 for l in items if l.indexed > 0),
            "injections": sum(l.injected for l in items),
            "recurred": sum(l.recurred for l in items),
        }


# ── 项目定位 ───────────────────────────────────────
def _same_path(a: Optional[Path], b: Optional[Path]) -> bool:
    """两个路径是否指向同一处。

    单独抽出来是为了**防御性**:`.resolve()` 在符号链接、
    Windows 短名(ADMINI~1)、权限异常上都可能抛或给出不同结果。
    比较失败时退回字符串比较,再失败就当不相等。
    """
    if a is None or b is None:
        return False
    try:
        if a.resolve() == b.resolve():
            return True
    except Exception:
        pass
    try:
        return str(a).lower() == str(b).lower()
    except Exception:
        return False


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    """从 cwd 向上找 .exp/。

    向上找而不是只看 cwd —— 从子目录启动 Claude 时也能激活。

    **必须排除全局层本身。** 全局层在 `~/.exp/`,它自己就是一个 `.exp/`
    目录 —— 向上遍历会在用户主目录处撞上它,于是【每个项目】都被当成
    "全局层的子项目",凭空多出一层指向同一目录的假项目层:
    经验被算两遍、注入两遍、计数记两遍。

    所以:找到的 `.exp/` 只要等于全局层,就跳过继续往上找。
    """
    cur = (start or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        cand = p / PROJECT_DIRNAME
        # 必须排除全局层。注意用 _same_path 而不是直接比较 ——
        # 符号链接 / Windows 短名 / 大小写都可能让"同一个目录"看起来不等,
        # 那种情况下误判会静默地把全局层当成项目层。
        if cand.is_dir() and not _same_path(cand, GLOBAL_DIR):
            return cand
    return None


def load_library(require: bool = False) -> Library:
    """加载两层经验库。**有多少层用多少层。**

    `require=True` 只在【该命令离开项目层就没有意义】时用
    (比如 distill —— 原始事件是项目本地的)。

    默认 False 很重要:全局层的卖点是"跨项目跟着人走",
    而一个没 `exp init` 过的项目恰恰是最需要它的场景。
    以前无条件要求项目层,导致 `exp add --layer global`
    在它最该起作用的场景下用不了。
    """
    root = find_project_root()
    if require and root is None:
        die(f"当前目录下没有 {PROJECT_DIRNAME}/。\n"
            f"  在项目里启用:   exp init\n"
            f'  或只记全局经验: exp add "<标题>" --layer global')
    return Library(root)


def require_any_layer(lib: Library) -> None:
    """一条经验都没有时给条能照做的出路,而不是干巴巴一句报错。"""
    if lib.layers:
        return
    die(f"没找到任何经验库。\n"
        f"  在项目里启用:   exp init\n"
        f'  或只记全局经验: exp add "<标题>" --layer global')


def session_id(payload: Optional[Dict[str, Any]] = None) -> str:
    """当前会话的标识。**注入记账和归因全靠它对上号。**

    取值的优先级,以及为什么只能这样排:

      1. hook 的 stdin payload —— Claude Code 每个 hook 事件都带
         `session_id`,这是**权威且必然存在**的来源。
      2. 环境变量 —— 手工跑 CLI(exp query / exp show)时没有 payload,
         只能靠它。注意这个变量**未必设了**,不能再往下假设。

    **环境变量的名字是 `CLAUDE_CODE_SESSION_ID`,不是 `CLAUDE_SESSION_ID`。**
    后者不存在,永远读不到东西。上一版只查了它,于是所有【手工调用】的
    注入都记在空会话名下 —— 而"模型主动查过没有"恰恰是靠手工调用
    (`exp query`)产生的信号,等于把要量的那个量本身弄丢了。
    `CLAUDE_CODE_SESSION_ID` 在 2.1.132+ 才加,所以两个都查。

    **绝不能用 `pid{getppid()}` 兜底。** 踩过的坑:上一版这么写过,
    而它是静默失效的 ——

      hook 命令是 `python3 x.py || python x.py`,中间隔着一层 shell。
      实测在 git-bash 下每次调用都新起一个中间 shell,于是 **ppid 每次
      都不同**(9024 / 10516 / 1848 …)。后果:

        第 1 次失败  → 注入记在 pid9024 名下
        第 2 次失败  → 去 pid10516 名下找注入记录 → 找不到
                     → 归因判定"模型没看过做法" → 记 missed

      于是 `recurred` **恒为 0**,所有签名精确命中的复发都被记成
      "触发词写偏了",gc 再据此让人去改一条本来没问题的 trigger。

    所以:**拿不到可靠会话就返回空串**,让归因显式地放弃判断 ——
    宁可暂时不归因,也不能拿一个错的 id 去污染计数。
    """
    if payload:
        sid = payload.get("session_id") or payload.get("sessionId")
        if sid:
            return _scrub(str(sid))
    return (os.environ.get("CLAUDE_CODE_SESSION_ID")   # 权威名字
            or os.environ.get("CLAUDE_SESSION_ID")     # 第三方发明的,通常恒空
            or os.environ.get("CLAUDE_SESSIONID")
            or "")


# ── 渲染 ───────────────────────────────────────────
def render_index(lib: Library, max_items: int = INDEX_MAX_ITEMS,
                 max_chars: int = INDEX_MAX_CHARS) -> Tuple[str, List[str]]:
    """生成注入用的紧凑索引。返回 (文本, 实际列出的 id)。

    **只注入标题 + trigger,不注入正文。**
    正文按需 `exp show <id>` 拉 —— 这就是渐进披露:
    索引常驻,正文按需。全局库攒到 500 条时这一步决定它还能不能用。

    返回实际列出的 id 很重要:索引有上限,没挤进去的经验
    不该被记成"注入过" —— 否则漏检归因就失真了。
    """
    items = lib.all()
    if not items:
        return "", []

    # 索引排序。**每一档都对应一种"它值得占一格"的理由**,顺序即优先级:
    #
    #   1. 高严重度   —— 撞上代价大
    #   2. 项目级     —— 只对这个仓库成立(与 query 打分一致,全局层降权)
    #   3. **用过的** —— 被证明能用上的,优先占位
    #   4. starved    —— 饿死过的,打破自我锁定
    #   5. created    —— 老的先来
    #   6. id         —— 兜底确定性
    #
    # 第 3 条的依据很直接:"库的价值不是条数,是被命中过多少条"。
    # 一条从来没人查过的经验,占着格子挤掉一条天天用的,是净损失。
    #
    # 第 4 条打破一个死循环:被容量挤掉的经验 indexed 恒为 0,
    # 若再让它排在最后,就"越饿死越靠后,越靠后越饿死"。
    #
    # **第 5 条是稳定性,不是美观。** 上一版拿 `l.title` 当最后的
    # tiebreak,于是同档条目按字符串排 —— `经验46` < `经验5` < `经验6`,
    # 跟新旧、重要性都无关。后果是**在中间插一条新经验会让整个
    # dropped 集合重新洗牌**,而 dropped 决定谁"从没露过面"。
    # 用 created 排序,新经验不会无故把老经验挤出去,截断结果可预测。
    def rank(l: Lesson):
        return (
            0 if l.severity == "high" else 1,
            0 if l.layer == "project" else 1,
            0 if l.injected > 0 else 1,
            0 if l.starved > 0 else 1,
            l.created or "9999",
            l.id,
        )

    items.sort(key=rank)

    st = lib.stats()
    lines = [
        "## 经验库(exp)",
        "",
        f"项目级 {st['project']} 条 / 全局级 {st['global']} 条。",
        "下面是大纲。**与当前任务相关的，先拉全文再动手**：",
        "`python exp.py show <id>`  ·  检索：`python exp.py query \"<你要做什么>\"`",
        "",
    ]
    used = sum(len(x) for x in lines)
    shown_ids: List[str] = []
    dropped_reason = ""
    for l in items:
        if len(shown_ids) >= max_items:
            dropped_reason = f"超出 {max_items} 条上限"
            break
        # 标注必须带上"这是哪一类问题"—— 只说"坏"模型会当噪声忽略,
        # 说了是哪一类它才知道该不该照做(rotten 的 fix 是明确不该照做的)。
        h = l.health()
        mark = f" [{HEALTH_LABEL[h]}]" if h != "ok" else ""
        flag = "" if l.status == "confirmed" else " (未验证)"
        row = (f"- **{l.title}**{flag}{mark}\n"
               f"  何时适用: {(l.trigger or '未填').splitlines()[0] if l.trigger else '未填'}\n")
        if used + len(row) > max_chars:
            dropped_reason = f"超出 {max_chars} 字符预算"
            break
        lines.append(row)
        used += len(row)
        shown_ids.append(l.id)

    # ── 截断必须【明确报告】────────────────────────
    #
    # 静默截断是这个系统最隐蔽的失效模式:被漏掉的经验永远不会被注入,
    # 于是它们的 missed 永远算不出来 —— 库里显示"一切正常",
    # 而实际上有一批经验从来没机会出现。
    #
    # **但"报告"不等于"警告"。** 上一版写的是"索引已满,跑 exp cluster
    # 看哪些该合并" —— 那是给**维护者**看的,而读这段文字的是**模型**,
    # 它既不会跑 cluster,也不会去合并经验。它只会把这句当噪声。
    #
    # 对模型有用的只有一件事:**知道库比它看到的列表大,以及怎么够到剩下的**。
    # 被截断的经验依然能被 `exp query` 检索到 —— 它们只是没常驻而已。
    # 这一点说清楚,截断就从"静默失效"变成了"已知的不完整"。
    dropped = len(items) - len(shown_ids)
    if dropped > 0:
        lines.append(
            f"\n> 这里只列出 {len(shown_ids)} 条({dropped_reason});"
            f"库里共 {len(items)} 条。"
            f"\n> 没列出的用 `exp query \"<你要做什么>\"` 一样能检索到。\n"
        )
    return "\n".join(lines), shown_ids


# ── 命令:init ──────────────────────────────────────
_INIT_GITIGNORE = """\
# 遥测不进版本库 —— 它们高频写,进 git 会天天冲突,
# 而且冲突发生在人类写的正文旁边。
# 经验本身(lessons/)要 commit:跨机器、跨协作者共享才是它的价值。
raw/
candidates.md
stats.json
injections.jsonl
index.json
.stats.lock
"""

_INIT_CONFIG = """\
# exp 配置
version: 1

# 检索返回条数上限
query_limit: 5

# 注入索引的字符预算
index_max_chars: 4000

# 会话启动时是否自动注入索引
inject_on_session_start: true
"""


def cmd_init(args: argparse.Namespace) -> int:
    """初始化**或修复** `.exp/`。

    **init 是幂等的修复命令,不只是首次安装。** `.exp/` 被误删、
    `lessons/` 被手滑删掉、git 没追踪空目录导致 clone 后缺目录 ——
    这些都不该让用户自己去 mkdir。

    所以:已存在的部分保留,缺的部分补齐。--force 只是换个说法,
    行为一样(不会删任何已有经验)。
    """
    root = Path(args.path or Path.cwd()).resolve() / PROJECT_DIRNAME
    existed = root.is_dir()
    if existed and args.force:
        print(f"已存在: {root}(补齐缺失部分,不会删已有经验)")

    layer = Layer(root, "project")
    # ensure 会补齐 lessons/ 和 raw/
    layer.ensure()

    # 这些文件必须落盘 —— git 不追踪空目录,否则用户 clone 下来
    # .exp/ 根本不存在,hook 检查失败,整套机制静默不启动。
    fixed = []
    for name, content in ((".gitignore", _INIT_GITIGNORE),
                          ("config.yaml", _INIT_CONFIG)):
        p = root / name
        if not p.exists():
            p.write_text(content, encoding="utf-8")
            fixed.append(name)

    if existed:
        print(f"[OK] 已就绪(补齐 {', '.join(fixed) if fixed else '缺失目录'}): {root}")
    else:
        print(f"[OK] 初始化完成: {root}")
    print(f"     项目级经验 → {root / 'lessons'}")
    print(f"     全局级经验 → {GLOBAL_DIR / 'lessons'}")

    # 顺手报一下数据层的健康问题 —— init 是用户刚接触这个工具时
    # 最可能跑的命令,在这里报比藏在 gc 里更容易被看到
    dup = layer.duplicate_ids()
    if dup:
        print()
        warn(f"发现 {len(dup)} 组重复的经验 id —— 按 id 取经验会失效:")
        for lid, files in list(dup.items())[:5]:
            print(f"       id「{lid}」→ {', '.join(files)}")

    print()
    print("   接下来说一句就够:")
    print('     exp add "标题" --trigger "当你正在做 X 时" --fix "..."')
    print("   或者让 hook 自己攒原始事件,事后跑 exp distill 归纳。")
    return 0


# ── 命令:hook ──────────────────────────────────────
def _scrub(s: str) -> str:
    """清掉字符串里的代理项(surrogates)。

    **这不是理论问题,是 hook 主路径上实测崩过的。**

    Claude Code 通过管道把工具信息喂给 hook。在中文 Windows 上,
    如果编码对不齐,读进来的字节会被解成 `\\udc80` 这类**代理字符**。
    它不能编码成 UTF-8 —— 于是 `json.dumps(...).write()` 抛
    `UnicodeEncodeError`,整个 post-failure 崩掉。

    后果很严重:**`post-failure` 挂在每一次工具失败上,它崩了
    等于归因彻底失效,而且是静默的** —— 你只会发现"坏经验怎么
    一直没被标出来"。

    所以:任何要落盘或做签名的字符串,先过这一道。
    """
    if not s:
        return s
    return s.encode("utf-8", "replace").decode("utf-8", "replace")


def _read_stdin_json() -> Dict[str, Any]:
    """读 hook 的 stdin payload。**任何情况下都不抛异常。**

    第一道防线是二进制读 + 容错解码,绕开 Windows 上按码页解码
    stdin 的老问题;第二道是 _scrub 清代理项。
    """
    try:
        buf = sys.stdin.buffer.read()
    except Exception:
        try:
            buf = sys.stdin.read().encode("utf-8", "replace")
        except Exception:
            return {}
    if not buf.strip():
        return {}
    # 先按 UTF-8 严格解,失败再按宽松策略 ——
    # 顺序很重要:严格解能成功时,宽松解会引入 {REPLACEMENT} 噪声
    for enc, err in (("utf-8", "strict"), ("utf-8", "replace"),
                     ("gbk", "replace")):
        try:
            d = json.loads(buf.decode(enc, err))
            return d if isinstance(d, dict) else {}
        except Exception:
            continue
    return {}


def cmd_hook(args: argparse.Namespace) -> int:
    """hook 入口。

    **未激活时静默退出。** 插件是全局安装的,但机制只在有 .exp/ 的
    项目里生效 —— 否则它会在无关项目里捕获噪声。
    静默是必须的:hook 的 stdout 会被塞进上下文,不能有杂音。

    **stdin 只读一次**,读到的 payload 在这里分发给各个 handler ——
    它是会话身份(`session_id`)的权威来源,而身份决定归因能不能对上号。
    每个 handler 各读一次 stdin 会读到空(流已经耗尽),那正是
    `pid{getppid()}` 那个 bug 的温床。
    """
    payload = _read_stdin_json()

    lib = Library(find_project_root())
    if not lib.enabled:
        return 0

    layer = lib.project_layer
    if layer is None:
        return 0          # enabled 已经保证了不会是 None,防御性兜底

    sid = session_id(payload)

    if args.event == "session-start":
        return _hook_session_start(lib, layer, sid)
    if args.event == "post-failure":
        return _hook_post_failure(lib, layer, payload, sid)
    if args.event == "user-prompt":
        return _hook_user_prompt(lib, layer, payload, sid)
    if args.event == "stop":
        return _hook_stop(lib, layer, payload, sid)
    return 0


def _save_in_layer(lib: Library, l: Lesson) -> None:
    """只用于 status 等【人决定、要 commit】的字段变更。
    计数器一律走 lib.bump(),不重写文件。
    """
    for layer in lib.layers:
        if layer.name == l.layer:
            layer.save(l)
            return


def _hook_session_start(lib: Library, layer: Layer, sid: str = "") -> int:
    block, ids = render_index(lib)
    if not block or not ids:
        return 0
    # level="index":模型看到的只有标题和触发词,没看到 fix。
    # 所以这次的注入只能用于判定 missed,不能用于判定 recurred。
    layer.log_injection(ids, "session-start:index", sid, level="index")
    # 一次写盘,不是 N 次 —— 见 Layer.bump_many
    #
    # 记的是 **indexed** 而不是 injected:"进过索引"和"看过正文"是
    # 两件事,分开记才分得清"死重"(有机会却没读)和"索引饿死"
    # (连机会都没有)。见 Lesson.is_starved_index。
    #
    # **没挤进去的也要记**(excluded)。这是"被容量挤掉"的**唯一确凿证据** ——
    # 它发生在渲染的那一刻,是事实而不是推断,所以立刻就能报,
    # 不用像死重那样等 30 天。
    shown = set(ids)
    lib.bump_all(
        (l, "indexed" if l.id in shown else "excluded", 1)
        for l in lib.all()
    )

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": block,
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _hook_post_failure(lib: Library, layer: Layer,
                       payload: Optional[Dict[str, Any]] = None,
                       sid: str = "") -> int:
    """失败发生的那一刻 —— 唯一适合记账的时机。

    这里做三件事,而且【全部无 LLM】:
      1. 算签名
      2. 归因:recurred / starved / missed
      3. 落原始事件 + 把做法送给模型

    ── 检索:只有一条路径,两档精度 ──────────────────
    上一版有个隐蔽的错位 —— **归因和投递各用各的检索器**:

        归因 → by_signature(精确)     投递 → query(模糊)

    于是一条挂了签名的经验可以被 by_signature 抓去归因,却因为
    二元组匹配不上 trigger 而**送不到模型手上**,然后被记成 missed。
    系统一边说"这条经验该出现",一边又不让它出现,还怪它的 trigger 写得不好。

    现在两处都走同一个查询函数,顺序也一致:先骨架,后语义。
    """
    payload = payload or {}
    # _scrub 是必须的:代理字符会让后面 json.dumps 写文件时抛异常,
    # 而这整条路径挂了 = 归因静默失效(详见 _scrub 的说明)
    tool = _scrub(str(payload.get("tool_name") or payload.get("tool") or "?"))
    err = payload.get("error") or payload.get("tool_response") or ""
    if isinstance(err, (dict, list)):
        try:
            err = json.dumps(err, ensure_ascii=False)
        except Exception:
            err = str(err)
    err = _scrub(str(err))
    code = _scrub(str(payload.get("exit_code", payload.get("error_type", ""))))
    sig = signature(tool, err, code)

    # 会话身份未知时无法判断"看没看过",归因退化为只认骨架命中。
    sess = sid or session_id()
    known_session = bool(sess)
    indexed = layer.recent_injections(sess, level="index")
    content = layer.recent_injections(sess, level="content")
    now = _now()

    # 所有计数攒起来,最后【一次写盘】—— 逐条 bump 是 N 次全文件读写
    pending: List[Tuple[Lesson, str, int]] = []
    promote: List[Lesson] = []
    touched: List[Lesson] = []

    # ── 顺序很关键:归因必须【先于】投递 ──────────────
    #
    # 两者都读 `content`(本会话送过的正文),而投递会往里写。
    # 如果先投递再归因,同一次失败就会看到"自己刚送出去的那条",
    # 于是把"失败发生时才第一次送达"误判成"看过做法还是犯" ——
    # 凭空造出一个 recurred。
    #
    # 正确语义是:**失败发生的那一刻,上下文里有什么**。
    # 所以先按这个快照归因,再把做法送出去。这决定了
    # `starved` 的含义 —— "这次失败时它还没送到过",而不是"永远没送到"。

    def _record(l: Lesson, mode: str) -> None:
        """按 mode × 看没看过 归因,并在该降级时登记。

        `seen_content` 为 None 时 attribute 会按模式保守处理 ——
        详见 Layer.attribute 的四个格子。
        """
        seen = (l.id in content) if known_session else None
        got = lib.attribute(l, mode, seen, pending)
        touched.append(l)
        if got == "recurred" and (l.recurred + 1 >= RECUR_THRESHOLD
                                  and l.status == "confirmed"):
            l.status = "needs_rewrite"
            promote.append(l)

    # ── 归因 A:骨架命中(确定性)──────────────────
    # 签名是结构化的(路径、数字、引号内容都归一化了),
    # 命中即"这是同一种失败" —— 换个措辞的同一个坑也能对上。
    for l in lib.by_signature(sig):
        _record(l, "skeleton")

    # ── 归因 B:语义命中(启发式兜底)────────────────
    # 签名只能靠 `exp distill` 从原始事件里抄,门槛很高,所以
    # 绝大多数经验是没签名的。只用 A 的话它们的 recurred 永远算不出来。
    #
    # 用失败信息本身当检索词。min_overlap 卡在 4(而非默认 2):
    # 归因写错比不写更坏 —— 误判会让人去"修"一条本来没问题的经验。
    # 这条判断对 B 成立;对 A 不成立,所以 A 不记 missed,见 Layer.attribute。
    if not touched and str(err).strip():
        try:
            for l in lib.relevant(f"{tool} {err}", limit=2):
                _record(l, "related")
        except Exception:
            pass

    # 计数一次落盘
    try:
        lib.bump_all(pending)
    except Exception:
        pass          # 归因失败绝不能影响 hook 本身

    # status 的变更要落盘(它是人可见的判断,进 commit);
    # 计数器不用 —— 它们在本机 stats.json 里。
    for l in promote:
        try:
            _save_in_layer(lib, l)
        except Exception:
            pass

    layer.append_raw({
        "ts": now,
        "kind": "failure",
        "session": sess,
        "tool": tool,
        "exit_code": code,
        "signature": sig,
        "error": str(err)[:600],
        "indexed": sorted(indexed),
        "content": sorted(content),
    })

    # ── 投递:把做法送到模型眼前 ────────────────────
    #
    # **必须走 stderr + 退出码 2。** PostToolUseFailure 的语义是
    # "exit 2 会把 stderr 送给 Claude";普通 stdout 到不了模型那里。
    # 这里不会阻断任何东西 —— 工具已经失败了,没有可阻断的动作。
    #
    # 查询顺序与上面归因一致:先骨架(精确),再语义(模糊)。
    # 这条通道是**唯一不依赖模型自觉**的正文投递 —— README 承诺
    # "这三件事不依赖任何人的自觉",而在此之前它恰恰没记账,
    # 于是模型真的读到了做法,系统却当没送过,recurred 永远算不出来。
    hint = lib.by_signature(sig)[:1]
    if not hint:
        try:
            hint = lib.query(f"{tool} {err}", limit=1, min_overlap=3)
        except Exception:
            hint = []
    if hint:
        l = hint[0]
        if l.fix:
            # 送达即记账 —— 否则上面那句承诺是空的
            layer.log_injection([l.id], "post-failure:hint", sess,
                                level="content")
            print(f"[exp] 这个坑记过:{l.title}\n"
                  f"  做法: {l.fix.splitlines()[0]}", file=sys.stderr)
            return 2
        return 0

    # ── 库里没有对应经验 —— 但 raw 知道这是不是重复的 ──────
    #
    # 这是投递路径上最大的一个漏洞,补上它靠的是一个**已经存在的事实**:
    #
    #   签名在【失败发生的那一刻】就算出来了,而且落进了 raw/。
    #   它只是从没被用来匹配 —— 匹配只发生在 `by_signature(经验)` 上,
    #   而那条路径要求经验**先挂上签名**。于是:
    #
    #     raw 里躺着"这是同一种失败"的结构性确证,
    #     投递却在用两个同样失效的启发式检索器找一条从没挂过它的经验。
    #
    # 后果是第 2 次踩坑和第 1 次【完全同形】:exit 0、什么都不送。
    # 系统手里握着"你犯过这个错"的硬证据,却保持沉默。
    #
    # 语义兜底(query, min_overlap=3)救不了这个 —— 实测 0/6:
    # 报错信息描述的是**症状**,trigger 描述的是**你正要做什么**,
    # 这两套词汇在设计上就不相交。签名之所以是"唯一的确定性匹配",
    # 正是因为它绕开了这个矛盾。
    #
    # 所以这里【不加新的启发式】,只把 raw 里的结构性事实接回来:
    # 同一签名出现到阈值就说话。这条判据冷启动期也能工作 ——
    # 它不要求库里先有任何东西。
    # **报错文本为空时不参与。** 那样的签名是退化的(`Bash||`),
    # 所有"没有报错的失败"都会塌缩到同一个签名上 —— 它不是
    # "同一种失败"的证据,是"没有信息"。在这里说话就是假阳性,
    # 而假阳性比不提示更坏:它让模型去记一条其实没发生过第二次的
    # "重复"。跟 min_overlap 卡在 4 是同一条原则。
    if not str(err).strip() or not str(sig).strip():
        return 0
    today = dt.date.today().isoformat()
    try:
        evs = [e for e in layer.raw_events(since=today)
               if str(e.get("ts", "")).startswith(today)]
    except Exception:
        return 0
    same = [e for e in evs
            if e.get("kind") == "failure" and e.get("signature") == sig]
    if len(same) < REPEAT_HINT_THRESHOLD:
        return 0
    # **只说一次。** 第 3、4 次再报就是纯噪声了 —— 模型已经知道了,
    # 而它此刻需要的是把精力花在解决问题上。用 raw 记一笔"提过了",
    # 不另开状态文件:raw 本来就是机器遥测的地方。
    if any(e.get("kind") == "repeat_hint" and e.get("signature") == sig
           for e in evs):
        return 0
    try:
        layer.append_raw({
            "ts": now, "kind": "repeat_hint", "session": sess,
            "signature": sig, "n": len(same),
        })
    except Exception:
        pass

    # 带工具名和报错原文 —— 签名里的 <STR> 是归一化过的,
    # 只给签名的话模型得自己猜它对应刚才哪条报错。
    first = str(err).splitlines()[0][:160] if str(err).strip() else ""
    print(f"[exp] 这个坑今天第 {len(same)} 次撞上,而库里没有对应经验:\n"
          f"  {sig}\n"
          + (f"  ← {first}   (工具: {tool})\n" if first else "")
          + "  现在记下来,下次再撞上就直接有做法了:\n"
          f'    exp add "<标题>" --from-raw "{sig}" \\\n'
          f'        --trigger "当你...时" --fix "<具体怎么做>"',
          file=sys.stderr)
    return 2


def render_lessons_for_prompt(hits: List[Lesson],
                              max_chars: int = PRE_ACTION_MAX_CHARS
                              ) -> Tuple[str, List[str]]:
    """把命中经验的**正文**渲染成任务前注入块。返回 (文本, 实际列出的 id)。

    和 `render_index` 的区别是这里的目的是**直接能用** ——
    不是"知道有这么条经验",而是"照这个做"。所以给全文三段
    (何时适用 / 根因 / 做法),而不是标题 + 触发词。

    **措辞刻意是陈述句,不是祈使句。** 注入的文本来自本地文件,
    但防御机制只看形状 —— "你必须在动手前做 X"这类命令式措辞
    容易撞上提示注入检测,反而让整段被丢掉。陈述事实不会有这个问题。

    返回实际列出的 id:被预算截掉的不该被记成注入过,
    否则 recurred 会算在一条模型根本没看到的经验头上。
    """
    if not hits:
        return "", []
    lines = [
        "## 经验库:与本次任务相关的条目(exp)",
        "",
        "检索到这几条历史和当前任务相关,列在下面供参考:",
        "",
    ]
    used = sum(len(x) for x in lines)
    shown: List[str] = []
    for l in hits:
        h = l.health()
        mark = f" [{HEALTH_LABEL[h]}]" if h != "ok" else ""
        flag = "" if l.status == "confirmed" else " (未验证)"
        body = [f"### {l.title}{flag}{mark}", ""]
        if l.trigger:
            body.append(f"- 何时适用: {l.trigger.splitlines()[0]}")
        if l.root_cause:
            body.append(f"- 根因: {l.root_cause.splitlines()[0]}")
        if l.fix:
            # 做法是这条经验的价值所在 —— 多给几行,其余段落不展开
            body.append("- 做法:")
            body.extend(f"    {ln}" for ln in l.fix.splitlines()[:6])
        body.append(f"- 全文: `exp show {l.id}`")
        body.append("")
        chunk = "\n".join(body) + "\n"
        if used + len(chunk) > max_chars and shown:
            break
        lines.append(chunk)
        used += len(chunk)
        shown.append(l.id)
    return "\n".join(lines), shown


def _hook_user_prompt(lib: Library, layer: Layer,
                      payload: Optional[Dict[str, Any]] = None,
                      sid: str = "") -> int:
    """任务前投递 —— **"按需加载"的那个"按需"。**

    ── 为什么必须是这个事件 ──────────────────────────

    这是唯一能真正在【动作之前】把经验送进上下文的通道。

    PreToolUse 看着更合适(它知道具体要跑什么),但它的
    `additionalContext` 是和工具结果**同一次**送达的 ——
    模型看到经验时,那条命令已经跑完了。这是 Messages API 的结构
    决定的:tool_result 必须紧跟 tool_use,中间没有位置可插。
    所以 PreToolUse 能改参数、能拦,但不能【提前告知】。

    UserPromptSubmit 在模型处理 prompt 之前触发,additionalContext
    随 prompt 进上下文。代价是它只看得到用户的自然语言,看不到
    tool_input —— 这个交换划算:看得见的意图远不如到得及的时机重要。

    ── 与 SessionStart 的分工 ────────────────────────

      SessionStart   全量索引(标题 + 触发词)  → 让模型【知道库存在】
      UserPromptSubmit 命中条目的全文          → 让模型【现在就能用】

    前者是"有这么些坑",后者是"这条和你正要做的有关,做法是这样"。
    """
    prompt = _scrub(str((payload or {}).get("prompt") or ""))
    if not prompt.strip():
        return 0

    # 长 prompt(贴进来的文件、大段日志)会让二元组命中率虚高 ——
    # 共同的常用字凑够阈值太容易了,那是假阳性不是相关。
    # 截断到前 2000 字符:意图通常在开头,而尾部是粘贴的内容。
    probe = prompt[:2000]

    # **本会话已经送过正文的,不再重复送。**
    # additionalContext 会留在会话历史里 —— 同一轮里反复注入同一条,
    # 不是提醒,是纯噪声,而且每轮都在烧上下文。
    seen = layer.recent_injections(sid, level="content") if sid else set()

    try:
        hits = lib.query(probe, limit=PRE_ACTION_MAX_LESSONS * 3,
                         min_overlap=PRE_ACTION_MIN_OVERLAP)
    except Exception:
        return 0

    # 三个过滤,每个都对应一种"送过去只有坏处"的情况:
    #   没 fix        —— 模型知道了也做不了什么,而正文长度的价值全在 fix
    #   已送过        —— 见上
    #   rotten        —— fix 已知不可执行(反复复发到阈值),不能当做法推
    hits = [l for l in hits
            if l.fix and l.id not in seen and not l.is_rotten]
    hits = hits[:PRE_ACTION_MAX_LESSONS]
    if not hits:
        return 0

    block, shown = render_lessons_for_prompt(hits)
    if not block or not shown:
        return 0

    # 记账必须是 **content** 级 —— 模型看到的是根因和做法,不是标题。
    # 这条记账让 recurred 有了全新的、更严格的含义:
    # "任务开始前就把做法给过你了,你还是踩了"。
    try:
        layer.log_injection(shown, "user-prompt:pre-action", sid,
                            level="content")
        lib.bump_all((l, "injected", 1) for l in hits if l.id in set(shown))
    except Exception:
        pass       # 记账失败不该影响投递

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block,
        }
    }, ensure_ascii=False))
    return 0


def _hook_stop(lib: Library, layer: Layer,
               payload: Optional[Dict[str, Any]] = None,
               sid: str = "") -> int:
    """收工时把本次会话的原始事件归拢成候选。

    **不调用 LLM。** 候选整理出来交给 agent 自己归纳 ——
    宿主里那个模型比任何外部调用都更懂当前语境。
    """
    sess = sid or session_id(payload or {})
    today = dt.date.today().isoformat()
    events = [e for e in layer.raw_events(since=today)
              if e.get("kind") == "failure"
              and (not sess or e.get("session") == sess)
              and str(e.get("ts", "")).startswith(today)]
    if len(events) < 2:
        return 0
    sigs: Dict[str, int] = {}
    for e in events:
        sigs[e.get("signature", "")] = sigs.get(e.get("signature", ""), 0) + 1
    repeated = {s: n for s, n in sigs.items() if n >= 2}
    if repeated:
        # **只提示,不写文件。**
        #
        # 上一版往 .exp/candidates.md 里追加,但没有任何地方读它 ——
        # 纯 write-only 死功能,还多一个要 gitignore 的文件。
        # 原始事件已经在 raw/ 里了,再抄一遍只是噪音。
        print(f"[exp] 本次会话有 {len(repeated)} 类重复失败。"
              f"跑 `exp distill` 归纳,或 `exp distill --write` 生成草稿。")
    return 0


# ── 命令:query / show / list ───────────────────────
def cmd_query(args: argparse.Namespace) -> int:
    lib = load_library()
    require_any_layer(lib)
    hits = lib.query(" ".join(args.context), limit=args.limit)
    if not hits:
        print("没有匹配的经验。")
        print("  (这不一定代表库里没有 —— 二元组检索匹配不到同义表达。")
        print("   换个说法,或者用 exp list 翻一遍。)")
        return 0
    layer = lib.project_layer
    if layer:
        # level="content":模型看到了根因和做法。只有这种注入之后还犯,
        # 才算得上"这条经验的 fix 不可执行"。
        layer.log_injection([l.id for l in hits], f"query:{' '.join(args.context)}",
                            session_id(), level="content")
        # injected 只计**正文**投递 —— 索引那次算 indexed,见 session-start。
        lib.bump_all((l, "injected", 1) for l in hits)
    for l in hits:
        tag = "全局" if l.layer == "global" else "项目"
        flag = "" if l.status == "confirmed" else f" [{l.status}]"
        print(f"── [{tag}] {l.title}{flag}")
        if l.trigger:
            print(f"   何时适用: {l.trigger}")
        if l.root_cause:
            print(f"   根因: {l.root_cause.splitlines()[0]}")
        if l.fix:
            for line in l.fix.splitlines()[:3]:
                print(f"   做法: {line}")
        if l.health() != "ok":
            print(f"   [!] 健康度: {l.health()}"
                  f" (索引{l.indexed}/注入{l.injected}/复发{l.recurred}"
                  f"/漏检{l.missed}/饿死{l.starved})")
        print(f"   全文: exp show {l.id}")
        print()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    lib = load_library()
    require_any_layer(lib)
    l = lib.get(args.lesson_id)
    if l is None:
        die(f"未找到: {args.lesson_id}")
    # 拉全文是内容级注入 —— 归因时要算进去
    if lib.project_layer:
        lib.project_layer.log_injection([l.id], f"show:{l.id}", session_id(),
                                        level="content")
        lib.bump(l, "injected")
    tag = "全局" if l.layer == "global" else "项目"
    print(f"# {l.title}")
    print(f"[{tag}] {l.category} · {l.status} · "
          f"{l.severity or '未标严重度'} · {l.health()}")
    print(f"索引 {l.indexed} / 注入 {l.injected} / 复发 {l.recurred} / "
          f"漏检 {l.missed} / 饿死 {l.starved} / 有用 {l.helped}")
    if l.trigger:
        print(f"\n## 何时适用\n\n{l.trigger}")
    for heading, attr in _SECTIONS[1:]:
        v = getattr(l, attr, "")
        if v:
            print(f"\n## {heading}\n\n{v}")
    # 相关是【算出来】的,不是存的 —— 见 Library.neighbors 的说明。
    nb = lib.neighbors(l, k=4)
    if nb:
        print("\n## 相关(按触发词相似度算的)")
        print()
        for sim, o in nb:
            tag = "全局" if o.layer == "global" else "项目"
            print(f"  {sim:.0%}  [{tag}] {o.title}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    lib = load_library()
    require_any_layer(lib)
    items = lib.all()
    if args.health:
        items = [l for l in items if l.health() == args.health]
    if args.layer:
        items = [l for l in items if l.layer == args.layer]
    if args.category:
        items = [l for l in items if l.category == args.category]
    if args.status:
        items = [l for l in items if l.status == args.status]
    if not items:
        print("(空)")
        return 0

    # ── 默认【按健康度分组】,不按主题 ──────────────
    #
    # 健康度是算出来的(不会腐烂),而且它才是你真正要动手处理的东西。
    # 主题分类需要"其他"桶,所以这里不给它位置。
    if args.by == "health":
        # **只用一份定义。** 这里原来是第三份手写的健康度映射,
        # 加了新档位忘了同步它 —— 于是 `exp list` 直接 KeyError 崩掉。
        # 分组顺序和文案统一从 HEALTH_ORDER / HEALTH_HEAD 取,
        # 那是 CLI、网页、gc 共用的唯一来源。
        order = {k: i for i, k in enumerate(HEALTH_ORDER)}
        label = HEALTH_HEAD
        items.sort(key=lambda l: (order.get(l.health(), 9),
                                  l.layer != "project", l.title))
        cur = None
        for l in items:
            h = l.health()
            if h != cur:
                cur = h
                n = sum(1 for x in items if x.health() == h)
                print(f"\n── {label[h]} ({n}) ──")
            print(f"  {(l.layer[:1].upper())} {l.title}")
            if l.trigger:
                print(f"      {l.trigger.splitlines()[0][:72]}")
    else:
        order = {c: i for i, c in enumerate(CATEGORIES)}
        items.sort(key=lambda l: (l.layer != "project",
                                  order.get(l.category, 99),
                                  _SEV_W.get(l.severity, 0) * -1,
                                  l.title))
        cur = None
        for l in items:
            key = (l.layer, l.category or "(未归类)")
            if key != cur:
                cur = key
                print(f"\n── {'项目级' if l.layer == 'project' else '全局级'}"
                      f" / {key[1]} ──")
            flags = []
            if l.status != "confirmed":
                flags.append(l.status)
            if l.health() != "ok":
                flags.append(HEALTH_LABEL.get(l.health(), l.health()))
            tail = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {l.severity or '-':6} {l.title}{tail}")
            print(f"         索引{l.indexed} 注入{l.injected} "
                  f"复发{l.recurred} 漏检{l.missed} 饿死{l.starved}")
    st = lib.stats()
    print(f"\n合计 {st['total']} 条 "
          f"(项目 {st['project']} / 全局 {st['global']}) · "
          f"确认 {st['confirmed']} · 复发 {st['rotten']} · "
          f"死重 {st['dead']}")
    if st.get("starved") or st.get("starved_index"):
        print(f"另有:投递饿死 {st['starved']} · 索引饿死 {st['starved_index']}"
              f"(这两类都【不是】经验本身的问题,别改 trigger)")
    return 0


# ── 命令:add ───────────────────────────────────────
def _raw_signature_index(layer: Layer) -> Dict[str, Dict[str, Any]]:
    """raw 里真实出现过的签名 → {n, sample, tool}。

    `--from-raw` 靠它校验。**抄错的签名是个静默的坏钩子** ——
    失败发生时 `by_signature` 找不到它,归因和投递都当这条经验不存在,
    而库里显示一切正常。跟 `missed` 是同一种失效模式,所以宁可报错。
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        events = [e for e in layer.raw_events() if e.get("kind") == "failure"]
    except Exception:
        return out
    for e in events:
        s = str(e.get("signature") or "")
        if not s:
            continue
        d = out.setdefault(s, {"n": 0, "sample": "", "tool": ""})
        d["n"] += 1
        if not d["sample"]:
            d["sample"] = str(e.get("error") or "")
            d["tool"] = str(e.get("tool") or "")
    return out


def cmd_add(args: argparse.Namespace) -> int:
    lib = load_library()
    layer = lib.project_layer if args.layer == "project" else lib.global_layer
    if layer is None:
        if args.layer == "global":
            # 全局层【自动引导】:没初始化过就直接建。
            # 这里不该报错 —— 全局层的卖点就是"跨项目跟着人走",
            # 而第一次用时它必然是空的。要求用户先跑个命令才能写第一条,
            # 是纯粹的人为摩擦。
            GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
            layer = Layer(GLOBAL_DIR, "global")
            layer.ensure()
            print(f"  (已创建全局层: {GLOBAL_DIR})")
        else:
            die(f"项目级不可用 —— 当前目录下没有 {PROJECT_DIRNAME}/。\n"
                f"  在项目里启用:   exp init\n"
                f'  或记到全局层:   exp add "<标题>" --layer global')
    if not args.trigger:
        warn("没填 --trigger,这条经验几乎检索不到 —— 检索靠它。")

    # ── 签名:手抄 + 从 raw 抄 ──────────────────────
    #
    # 签名是【唯一确定性】的匹配依据,但只能从 `exp distill` 的输出里
    # 手抄 —— 门槛高到大多数经验干脆不挂。`--from-raw` 去掉这一步。
    sigs = list(args.signature or [])
    wanted = list(getattr(args, "from_raw", None) or [])
    if wanted:
        known = _raw_signature_index(layer)
        for s in wanted:
            if s not in known:
                near = [k for k in known if s[:24] in k] or list(known)[:10]
                die(f"raw 里没有这个签名:\n    {s}\n"
                    f"  抄错的签名是个永远不命中的【静默】坏钩子 ——\n"
                    f"  失败时匹配不上,系统当这条经验不存在。\n"
                    f"  raw 里现有 {len(known)} 类签名:\n"
                    + "".join(f"    {k}\n" for k in near)
                    + "  完整列表:`exp distill`")
            if s not in sigs:
                sigs.append(s)

    l = Lesson(
        id=_slug(args.title),
        layer=layer.name,
        title=args.title,
        category=args.category,
        status=args.status,
        severity=args.severity,
        trigger=args.trigger or "",
        symptom=args.symptom or "",
        root_cause=args.root_cause or "",
        fix=args.fix or "",
        evidence=args.evidence or "",
        scope=args.scope or "",
        signatures=sigs,
        created=dt.date.today().isoformat(),
    )
    if (layer.lessons_dir / f"{l.id}.md").exists():
        die(f"已存在: {l.id}.md\n  换个标题,或直接编辑该文件。")
    layer.save(l)
    print(f"[OK] [{layer.name}] {l.id}")
    print(f"     {l.path}")
    return 0


# ── 命令:distill / feedback / gc ───────────────────
def cmd_distill(args: argparse.Namespace) -> int:
    """把原始事件归拢成候选,交给 agent 归纳。

    **这一步不调用 LLM。** 它只做聚类,产出的事实交给宿主模型 ——
    宿主模型有当前会话的完整语境,归纳质量高于任何外部调用。
    """
    # 原始事件是【项目本地】的 —— 这里离开项目层没有意义,所以要 require
    lib = load_library(require=True)
    layer = lib.project_layer
    if layer is None:
        die("distill 需要项目层。先跑:exp init")
    events = [e for e in layer.raw_events() if e.get("kind") == "failure"]
    if not events:
        print("没有原始事件。hook 还没捕获到失败,或者 raw/ 被清了。")
        return 0

    sigs: Dict[str, Dict[str, Any]] = {}
    for e in events:
        s = e.get("signature", "")
        if not s:
            continue
        d = sigs.setdefault(s, {"n": 0, "first": e.get("ts", ""),
                                "last": e.get("ts", ""), "sample": ""})
        d["n"] += 1
        d["last"] = e.get("ts", "")
        if not d["sample"]:
            d["sample"] = e.get("error", "")

    known = {sig for l in lib.all() for sig in l.signatures}
    new = {s: d for s, d in sigs.items() if s not in known}
    recurring = {s: d for s, d in new.items() if d["n"] >= 2}

    print(f"原始事件 {len(events)} 条,签名 {len(sigs)} 类。")
    print(f"其中 {len(new)} 类没有对应经验,{len(recurring)} 类重复出现。\n")

    if not new:
        print("所有失败都已有对应经验。跑 exp list --health missed 看检索漏了哪些。")
        return 0

    print("── 候选(按出现次数)──\n")
    top = sorted(new.items(), key=lambda x: -x[1]["n"])[:20]
    for s, d in top:
        print(f"  x{d['n']}  {s[:90]}")
        if d["sample"]:
            print(f"        {d['sample'][:110]}")

    # ── --write:直接产出【可编辑的草稿文件】────────────────
    #
    # 只打印一段"你去写吧"的命令是不够的 —— 那让 distill 变成
    # 一个只读报告,而归纳这一步永远没人做。
    # 草稿写出来,填空比从零写容易得多。
    if args.write:
        draft = layer.root / "candidates.md"
        blocks = [f"# 待归纳的候选({dt.date.today().isoformat()})\n",
                  "\n> 填好 `trigger` 和「做法」之后,用 `exp add` 存进经验库。\n"
                  "> **trigger 要写成「当你正在做 X 时」** —— 检索靠它。\n"]
        for s, d in top:
            blocks.append(
                f"\n## 候选:出现 {d['n']} 次\n\n"
                f"```\n样本错误:{(d['sample'] or '')[:300]}\n签名: {s}\n```\n\n"
                f"trigger: 当你...时\n\n"
                f"根因: \n\n"
                f"做法: \n\n"
                f"exp add \"<标题>\" --trigger \"...\" --root-cause \"...\" "
                f"--fix \"...\" --signature \"{s}\"\n")
        try:
            draft.write_text("\n".join(blocks), encoding="utf-8")
            print(f"\n  [OK] 草稿已写入:{draft}")
            print(f"       填好之后用 exp import 或 exp add 存进库。")
            print(f"       (这个文件在 .gitignore 里,是工作区不是资产)")
        except Exception as e:
            warn(f"写草稿失败: {e}")
        return 0

    print()
    print("  归纳成经验。**把签名一起挂上** —— 挂上之后,这种失败再出现")
    print("  会被自动记为复发,不需要任何人再去判断一遍:\n")
    if top:
        print(f'    exp add "<标题>" \\')
        print(f'        --trigger "当你...时" --root-cause "..." --fix "..." \\')
        print(f'        --signature "{top[0][0]}"')
    print()
    print("  或者直接生成可编辑的草稿:")
    print("      exp distill --write      # 写到 .exp/candidates.md")
    print()
    print("  也可以把上面这段交给 agent,让它读完 raw/ 再归纳 ——")
    print("  它比你更清楚当时发生了什么。")
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    """显式反馈。用于模型/人明确知道"这条有用"或"这条是错的"。"""
    lib = load_library()
    l = lib.get(args.lesson_id)
    if l is None:
        die(f"未找到: {args.lesson_id}")
    if args.outcome in ("helped", "recurred"):
        lib.bump(l, args.outcome)
    elif args.outcome in ("confirmed", "refuted"):
        l.status = args.outcome
        _save_in_layer(lib, l)     # status 要 commit,计数器不用
    else:
        die(f"未知 outcome: {args.outcome}(可用 helped/recurred/confirmed/refuted)")
    print(f"[OK] {l.id} → helped={l.helped} recurred={l.recurred} status={l.status}")
    return 0


def _index_capacity(lib: Library) -> Dict[str, Any]:
    """索引装得下吗?

    这是【最隐蔽的失效模式】的可视化:索引满了 → 有经验永远不会被注入
    → 它们的 missed 永远算不出来 → 库里显示"一切正常",
    而实际有一批经验从来没机会出现。

    `exp gc` / `exp cluster` / 网页面板都报它 —— 三个界面必须对
    "库里有没有问题"给出同一个答案。
    """
    _, shown = render_index(lib)
    total = len([l for l in lib.all()])
    # **报出真正生效的那条约束。** 上一版固定写"条数上限 120",
    # 而实际卡住的往往是字符预算 —— 报错的限制器会把人引向错误的处置。
    if len(shown) >= INDEX_MAX_ITEMS:
        bound = f"最多 {INDEX_MAX_ITEMS} 条"
    else:
        bound = f"{INDEX_MAX_CHARS} 字符预算"
    return {
        "capacity": INDEX_MAX_ITEMS,
        "chars": INDEX_MAX_CHARS,
        "bound": bound,
        "total": total,
        "shown": len(shown),
        "dropped": max(0, total - len(shown)),
    }


def cmd_gc(args: argparse.Namespace) -> int:
    """体检:把坏经验和死重列出来。

    **不自动删除。** 报告,不处置 —— 库的改动应该有人过目。
    """
    lib = load_library()
    st = lib.stats()
    capacity = _index_capacity(lib)
    print(f"合计 {st['total']} 条(项目 {st['project']} / 全局 {st['global']})")
    print(f"确认 {st['confirmed']} · 未验证 {st['hypothesis']} · "
          f"已推翻 {st['refuted']}")
    print()

    rotten = [l for l in lib.all() if l.health() == "rotten"]
    missed = [l for l in lib.all() if l.health() == "missed"]
    starved = [l for l in lib.all() if l.health() == "starved"]
    dead = [l for l in lib.all() if l.health() == "dead"]
    starved_index = [l for l in lib.all() if l.health() == "starved_index"]

    if rotten:
        print(f"── 反复复发 ({len(rotten)}) —— 注入了还是犯,fix 多半不可执行 ──")
        for l in rotten:
            print(f"  {l.title}")
            print(f"    索引{l.indexed} 注入{l.injected} 复发{l.recurred} · {l.path}")
        print()
    if missed:
        print(f"── 检索漏检 ({len(missed)}) —— trigger 写偏了,该命中没命中 ──")
        for l in missed:
            print(f"  {l.title}  (漏检{l.missed})")
            print(f"    现在写的触发词: {l.trigger[:70]}")
        print()
    if starved:
        print(f"── 投递饿死 ({len(starved)}) —— 签名精确命中过,做法却没送到模型手上 ──")
        print(f"  注意:这类**不是** trigger 的问题,签名是精确匹配的,"
              f"改 trigger 没有用。")
        for l in starved:
            print(f"  {l.title}  (饿死{l.starved} · 注入{l.injected})")
            print(f"    签名: {', '.join(l.signatures[:2]) or '无'}")
        print("  处置:查为什么没送达 —— 通常是会话身份丢失,"
              "或投递通道没记账。")
        print()
    if starved_index:
        print(f"── 索引饿死 ({len(starved_index)}) —— 索引装不下,"
              f"它们从没露过面 ──")
        print(f"  索引上限 {INDEX_MAX_ITEMS} 条。这些**被挤掉过**"
              f"(excluded),一次都没进去:",
              f"{starved_index[0].excluded} 次" if len(starved_index) == 1
              else f"最多的被挤掉 {max(l.excluded for l in starved_index)} 次")
        for l in starved_index:
            print(f"  {l.title}  (被挤掉 {l.excluded} 次)")
        print("  处置:扩容量、提优先级,或该上分层了 —— **不是**删经验。")
        print()
    # ── 容量告警:今天就报,不等 30 天 ─────────────────
    #
    # 上面那类是"已经确认被挤掉过"的个体。这里报的是**结构本身** ——
    # 只要库比容量大,就一定有人排不进去,哪怕它今天才刚写。
    #
    # 为什么必须当场报:容量溢出是**算术事实**,不是使用模式。
    # 50 条经验、上限 40 条,今天就排除了 10 条 —— 跟它们放了多久无关。
    # 等 30 天再报,中间这段时间用户看到的是一切正常,
    # 而这正是 README 里说的那种最隐蔽的失效模式(信号算不出来 ≠ 没事)。
    if capacity["dropped"] > 0:
        print(f"── 容量告警 —— 库比索引大,必有经验排不进去 ──")
        print(f"  {capacity['total']} 条经验,索引受限于 {capacity['bound']},"
              f"当前列出 {capacity['shown']} 条,"
              f"排除 {capacity['dropped']} 条。")
        print(f"  被排除的仍能被 `exp query` 检索到 —— 只是不常驻。")
        print(f"  但它们的 missed 永远算不出来(主动检索不会跑到它们),")
        print(f"  所以【库里没问题】是假象。")
        print(f"  处置:条数受限于字符预算就调高 INDEX_MAX_CHARS;")
        print(f"        确实需要更多条目就调 INDEX_MAX_ITEMS;")
        print(f"        或者把通用的经验挪到全局层、合并同类。")
        print()
    if dead:
        print(f"── 死重 ({len(dead)}) —— 进过索引,{DEAD_WEIGHT_DAYS} 天没被拉过全文 ──")
        for l in dead:
            print(f"  {l.title}  (创建 {l.created} · 进过索引 {l.indexed} 次)")
        print()

    # ── 数据健康 ────────────────────────────────────
    # 这两类问题不属于"经验的内容问题",但会让库悄悄失灵,
    # 所以放在体检里一起报。
    dup: Dict[str, List[str]] = {}
    unreadable: List[Path] = []
    for lay in lib.layers:
        dup.update(lay.duplicate_ids())
        for p in lay.paths():
            try:
                fm, _ = _split_frontmatter(
                    p.read_text(encoding="utf-8", errors="replace"))
                if not fm.strip() or not _load_yaml(fm):
                    unreadable.append(p)
            except Exception:
                unreadable.append(p)

    if dup:
        print(f"── 重复的经验 id ({len(dup)} 组) —— 按 id 取经验会失效 ──")
        for lid, files in list(dup.items())[:8]:
            print(f"  「{lid}」")
            for f in files:
                print(f"      {f}")
        print("  修法:把其中一条的 id / title 改掉,或合并两条。")
        print()
    if unreadable:
        print(f"── 读不出 frontmatter 的文件 ({len(unreadable)}) —— 会被当空经验 ──")
        for p in unreadable[:8]:
            print(f"  {p}")
        print("  修法:补上 `---` 包裹的 frontmatter,或删掉该文件。")
        print()

    if not (rotten or missed or starved or starved_index or dead
            or dup or unreadable):
        print("没有需要处理的。")
    else:
        print("处置建议:")
        print("  复发     → 重写 fix,让它可执行;或降级为 hypothesis")
        print("  漏检     → 重写 trigger,用你实际会想到的词")
        print("  投递饿死 → 查投递链路(会话身份 / 记账),**不要**改 trigger")
        print("  索引饿死 → 扩容量或提优先级,**不要**删经验")
        print("  死重     → 删掉,或合并进相近的经验")
        print("  重名     → 改 id 或合并")
    return 0


def cmd_cluster(args: argparse.Namespace) -> int:
    """按触发词相似度聚类 —— **给人看的,不落盘。**

    这个命令回答的是整理问题,不是检索问题:

      「我这 40 条里,有哪些其实是同一个原则?」
      「哪两条该合并?哪条其实是多余的?」

    **它不产生永久结构。** 聚类不稳定 —— 加一条经验整个分组会移位。
    所以它每次现算,结果只在屏幕上,不进文件。

    什么时候该用它:**索引开始截断的时候。**
    `render_index` 有 40 条 / 4000 字符的上限,超了就漏;
    漏掉的经验永远不会被注入,`missed` 也就永远算不出来。
    那才是需要聚类的真实信号 —— 不是"我觉得该分类了"。
    """
    lib = load_library()
    require_any_layer(lib)
    items = [l for l in lib.all() if l.status != "refuted"]
    n = len(items)
    if n < 2:
        print("经验太少,聚类没有意义。")
        return 0

    # 索引容量 —— 判断"该不该上聚类"的可测量依据
    block, shown = render_index(lib)
    truncated = n - len(shown)

    # 两件事要分开说 —— 它们在不同的 N 上变成问题:
    #
    #   检索容量:索引装不下才需要聚类做分层(N > INDEX_MAX_ITEMS)
    #   整理维护:找重复/该合并的,任何 N 都有价值
    print(f"经验 {n} 条 · 索引容量 {INDEX_MAX_ITEMS} 条")
    if truncated > 0:
        print(f"[!] 索引截断了 {truncated} 条 —— 这些经验【永远不会被注入】,"
              f"missed 也永远算不出来。")
        print(f"    检索层需要分层了:按聚类摘要注入,展开按需。")
    else:
        print(f"    索引装得下(N ≤ {INDEX_MAX_ITEMS}),检索不需要分层 —— "
              f"平铺就是最优解。")
    print(f"    但【整理】在任何规模下都有价值:下面的重复检测与容量无关。")
    print()

    groups = lib.clusters(min_sim=args.min_sim)
    if not groups:
        print("没有形成任何聚类(相似度都没到阈值)。这是好事 ——")
        print("说明经验之间彼此独立,没有冗余。")
    else:
        print(f"── 主题聚类({len(groups)} 组,按相似度聚合,非永久标签)──\n")
        for i, g in enumerate(groups, 1):
            print(f"  [{i}] {len(g)} 条")
            for l in g:
                print(f"      {l.title}")
            print()

    merges = lib.merge_candidates(min_sim=args.merge_sim)
    if merges:
        print(f"── 建议合并({len(merges)} 对,相似度 ≥ {args.merge_sim:.0%})──\n")
        for sim, a, b in merges[:15]:
            print(f"  {sim:.0%}  {a.title}")
            print(f"        {b.title}")
        print()
        print("  合并的做法:把两条的做法字段并进一条,另一条设 superseded_by,")
        print("  而不是直接删 —— 删了你可能重新相信那个已经被推翻的版本。")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """这台机器上 exp 到底处于什么状态。

    **卸载前必须能回答"我的数据在哪、删了会丢什么"。**
    用户不会永远按你设想的方式使用 —— 插件卸载后 .exp/ 会留在
    项目里,没人知道该不该删、删了会不会丢东西。
    """
    print("── 安装状态 ──\n")
    print(f"  插件位置   {Path(__file__).resolve().parent}")
    print(f"  经验格式   schema v{SCHEMA_VERSION}")

    print("\n── 数据位置 ──\n")
    proj = find_project_root()
    if proj:
        n = len(Layer(proj, "project").paths())
        print(f"  项目层     {proj}   ({n} 条)  ← 跟着这个 repo 走")
    else:
        print(f"  项目层     (当前目录树里没有 {PROJECT_DIRNAME}/)")

    if GLOBAL_DIR.is_dir():
        n = len(Layer(GLOBAL_DIR, "global").paths())
        if _same_path(proj, GLOBAL_DIR):
            print(f"  全局层     {GLOBAL_DIR}   ({n} 条)  "
                  f"← 你正站在全局层目录里")
        else:
            print(f"  全局层     {GLOBAL_DIR}   ({n} 条)  ← 跟着这台机器/这个人走")
    else:
        print(f"  全局层     (未创建: {GLOBAL_DIR})")

    print("\n── 卸载指引 ──\n")
    print("  1. 去掉 hooks:卸载插件,或从 ~/.claude/settings.json 删掉那三条")
    print("  2. 数据【不会】被自动删除。要删哪些自己决定:")
    print()
    print("       项目层  <项目>/.exp/    —— 建议保留:它要 commit、团队共享")
    print("       全局层  ~/.exp/         —— 只影响你本机")
    print()
    print("  3. 删之前先看一眼有什么:")
    print("       exp list --layer global")
    print()
    print("  **不建议直接删项目层的 lessons/** —— 那些是 commit 过的资产,")
    print("  删掉等于回滚团队的经验。要停用就卸载 hook,数据留着。")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """把经验文件升级到当前 schema 版本。

    **为什么必须有这个命令:** 经验是用户的数据资产。插件升级时如果
    格式变了而没有迁移路径,用户只能靠猜 —— 而猜错 = 弄坏数据。

    现在只做一件事:给缺 `schema:` 字段的文件补上版本号(视为 0 → 1)。
    将来格式再变时,在这里加按版本的转换分支。

    默认 **dry-run**,加 --apply 才真写。
    """
    lib = load_library()
    require_any_layer(lib)
    stale: List[Lesson] = [l for l in lib.all() if l.schema < SCHEMA_VERSION]

    if not stale:
        print(f"全部 {len(lib.all())} 条已是 schema v{SCHEMA_VERSION},无需迁移。")
        return 0

    print(f"有 {len(stale)} 条低于 v{SCHEMA_VERSION}:")
    print()
    for l in stale:
        print(f"  v{l.schema}  {l.title}")
        print(f"        {l.path}")
    print()

    if not args.apply:
        print(f"这是 dry-run。加 --apply 才会写入。")
        print(f"  迁移内容:补 `schema: {SCHEMA_VERSION}` 字段,正文不动。")
        return 0

    n = 0
    for l in stale:
        l.schema = SCHEMA_VERSION
        try:
            _save_in_layer(lib, l)     # 走正常的写盘路径(含回读校验)
            n += 1
        except Exception as e:
            warn(f"迁移失败 {l.id}: {e}")
    print(f"[OK] 迁移 {n} 条 → v{SCHEMA_VERSION}")

    # 磁盘缓存里存的是旧结构,清掉让它重建
    for layer in lib.layers:
        try:
            (layer.root / "index.json").unlink(missing_ok=True)
        except Exception:
            pass
    print("     已清解析缓存,下次读会重建。")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """从旧的 _ops/lessons/*.yaml 批量导入。

    存在的理由:迁移不该是手工活。你已有的语料是真实资产。
    """
    src = Path(args.source)
    if not src.is_dir():
        die(f"目录不存在: {src}")
    lib = load_library()
    layer = lib.project_layer if args.layer == "project" else Layer(
        GLOBAL_DIR, "global")
    layer.ensure()

    n = 0
    for p in sorted(src.glob("*.y*ml")):
        if p.name.startswith("_"):
            continue
        d = _load_yaml(p.read_text(encoding="utf-8", errors="replace"))
        if not d.get("title") and not d.get("id"):
            continue
        lid = _slug(str(d.get("title") or d.get("id") or p.stem))
        if (layer.lessons_dir / f"{lid}.md").exists() and not args.force:
            print(f"  跳过(已存在): {lid}")
            continue
        l = Lesson(
            id=lid,
            layer=layer.name,
            title=str(d.get("title") or lid),
            category=str(d.get("category") or "其他"),
            status=str(d.get("status") or "hypothesis"),
            severity=str(d.get("severity") or ""),
            trigger=str(d.get("trigger") or "").strip(),
            symptom=str(d.get("symptom") or "").strip(),
            root_cause=str(d.get("root_cause") or "").strip(),
            fix=str(d.get("fix") or "").strip(),
            evidence=str(d.get("evidence") or "").strip(),
            scope=str(d.get("scope") or "").strip(),
            related=[str(x) for x in (d.get("related") or [])],
            created=str(d.get("created") or dt.date.today().isoformat()),
        )
        layer.save(l)
        n += 1
    print(f"[OK] 导入 {n} 条 → {layer.lessons_dir}")
    return 0


# ── 命令:serve(网页) ──────────────────────────────


def cmd_serve(args: argparse.Namespace) -> int:
    """起一个本地网页,展示经验库。

    零依赖:http.server + 一个静态页。没有前端构建步骤 ——
    一个需要 npm install 的可视化工具,在你想看它的时候多半是坏的。
    """
    import http.server
    import socketserver
    import threading
    import webbrowser

    # 启动时先探一次,用来打印信息 / 尽早报错
    lib = load_library()
    web_dir = Path(__file__).parent / "web"

    def payload() -> Dict[str, Any]:
        # **每次请求都重建 Library。**
        #
        # 不能在启动时绑定一次:Library.layers 是构造时固定的,
        # 而 serve 是长驻进程 —— 跑起来之后才出现的 ~/.exp/
        # (比如另一个会话记了条全局经验)会永远看不到,直到重启。
        # 解析有磁盘 + 进程缓存兜底,重建的代价可以忽略。
        lib = load_library()
        items = []
        for l in lib.all():
            items.append({
                "id": l.id, "title": l.title, "layer": l.layer,
                "category": l.category, "status": l.status,
                "severity": l.severity, "trigger": l.trigger,
                "symptom": l.symptom, "root_cause": l.root_cause,
                "fix": l.fix, "evidence": l.evidence, "scope": l.scope,
                "related": l.related, "created": l.created,
                "injected": l.injected, "indexed": l.indexed,
                "recurred": l.recurred,
                "missed": l.missed, "starved": l.starved,
                "helped": l.helped,
                "last_injected": l.last_injected,
                "last_recurred": l.last_recurred,
                "health": l.health(),
                "health_label": HEALTH_LABEL.get(l.health(), ""),
                "health_action": HEALTH_ACTION.get(l.health(), ""),
            })
        return {
            "stats": lib.stats(),
            "lessons": items,
            # 聚类和邻居都是【现算】的,不是存的 —— 见 Library.clusters。
            # 页面和 CLI 必须看到同一套真相,否则两个界面会给出
            # 不同的"哪些是重复的"结论。
            "clusters": [
                {"ids": [l.id for l in g]} for g in lib.clusters()
            ],
            "neighbors": {
                l.id: [[round(s, 3), o.id] for s, o in lib.neighbors(l, k=4)]
                for l in lib.all()
            },
            "index": _index_capacity(lib),
            "events": lib.project_layer.raw_events()[-200:]
            if lib.project_layer else [],
            "roots": {
                "project": str(lib.project_layer.root) if lib.project_layer else "",
                "global": str(GLOBAL_DIR),
            },
        }

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(web_dir), **kw)

        def log_message(self, *a):    # 静音,别刷屏
            pass

        def do_GET(self):
            if self.path.startswith("/api/data"):
                body = json.dumps(payload(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            return super().do_GET()

    port = args.port
    for attempt in range(20):
        try:
            httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        die("找不到可用端口")

    url = f"http://127.0.0.1:{port}/"
    print(f"[OK] 经验库面板: {url}")
    print(f"     项目级 {lib.project_layer.root if lib.project_layer else '(无)'}")
    print(f"     全局级 {GLOBAL_DIR}")
    print("     Ctrl-C 停止")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


# ── 命令:pack(经验包) ─────────────────────────
#
# 经验包解决的是【冷启动】:一个空库检索不出任何东西,而没人愿意
# 在早期往里写。通用型的坑(跨项目都成立的那些)可以先打包发出去,
# 让别人一装就有东西用。
#
# 这也是"通用经验库"做不到的事 —— 项目私有的经验不可共享,
# 所以那个方向永远逃不出冷启动。
def packs_dir() -> Path:
    """经验包目录。**必须在插件目录内部。**

    plugins/exp/packs/   ← 随插件分发,装完就在
    <repo>/packs/        ← 开发时的回退(插件目录内没有才用)

    教训:最初只放在仓库根,而插件安装后用户拿到的是 plugins/exp/ ——
    `../../packs` 在用户机器上不存在,于是"解决冷启动"的核心卖点
    在真实安装路径下直接失效。
    """
    here = Path(__file__).resolve().parent
    inside = here / "packs"
    if inside.is_dir():
        return inside
    return here.parent.parent / "packs"


def _pack_lessons(p: Path) -> List[Path]:
    d = p / "lessons"
    if not d.is_dir():
        return []
    return [f for f in sorted(d.glob("*.md")) if not f.name.startswith("_")]


def cmd_pack(args: argparse.Namespace) -> int:
    if args.pack_action == "list":
        base = Path(args.from_dir) if args.from_dir else packs_dir()
        if not base.is_dir():
            die(f"找不到经验包目录: {base}")
        found = False
        for p in sorted(base.iterdir()):
            if not p.is_dir() or p.name.startswith("_"):
                continue
            files = _pack_lessons(p)
            if not files:
                continue
            found = True
            desc = ""
            if (p / "README.md").exists():
                first = (p / "README.md").read_text(
                    encoding="utf-8", errors="replace").splitlines()
                for line in first:
                    if line.strip() and not line.startswith("#"):
                        # 去掉 markdown 标记 —— 这是给人扫一眼的摘要行
                        desc = re.sub(r"[*`_]", "", line).strip()
                        break
            print(f"  {p.name:<20} {len(files)} 条  {desc[:60]}")
        if not found:
            print(f"  (在 {base} 下没有找到经验包)")
        return 0

    if args.pack_action == "install":
        base = Path(args.from_dir) if args.from_dir else packs_dir()
        src = base / args.name
        files = _pack_lessons(src)
        if not files:
            die(f"经验包不存在或为空: {src}")
        lib = load_library()
        layer = lib.global_layer if args.layer == "global" else lib.project_layer
        if layer is None:
            if args.layer == "global":
                GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
                layer = Layer(GLOBAL_DIR, "global")
            else:
                die("项目级不可用。先跑 exp init")
        layer.ensure()
        n_skip = n_add = 0
        for f in files:
            dst = layer.lessons_dir / f.name
            if dst.exists() and not args.force:
                n_skip += 1
                continue
            # 逐条解析再写,走的是和 add 一样的序列化 + 回读校验路径 ——
            # 装在别人机器上的经验也不能是坏的。
            fm, body = _split_frontmatter(
                f.read_text(encoding="utf-8", errors="replace"))
            d = _load_yaml(fm)
            lid = str(d.get("id") or f.stem)
            l = Lesson(
                id=lid, layer=layer.name,
                title=str(d.get("title") or lid),
                category=str(d.get("category") or "其他"),
                status=str(d.get("status") or "hypothesis"),
                severity=str(d.get("severity") or ""),
                trigger=str(d.get("trigger") or "").strip(),
                related=[str(x) for x in (d.get("related") or [])],
                created=str(d.get("created") or dt.date.today().isoformat()),
                symptom=_parse_body(body).get("表现", ""),
                root_cause=_parse_body(body).get("根因", ""),
                fix=_parse_body(body).get("做法", ""),
                evidence=_parse_body(body).get("证据", ""),
                scope=_parse_body(body).get("适用范围", ""),
            )
            layer.save(l)
            n_add += 1
        print(f"[OK] 安装经验包 {args.name} → {layer.name} 层")
        print(f"     新增 {n_add} 条" + (f",跳过 {n_skip} 条(已存在)" if n_skip else ""))
        print(f"     {layer.lessons_dir}")
        return 0

    if args.pack_action == "show":
        base = Path(args.from_dir) if args.from_dir else packs_dir()
        src = base / args.name
        files = _pack_lessons(src)
        if not files:
            die(f"经验包不存在或为空: {src}")
        if (src / "README.md").exists():
            print((src / "README.md").read_text(encoding="utf-8", errors="replace"))
            print()
        print(f"── 包含 {len(files)} 条 ──")
        for f in files:
            fm, _ = _split_frontmatter(
                f.read_text(encoding="utf-8", errors="replace"))
            d = _load_yaml(fm)
            sev = d.get("severity") or "-"
            print(f"  [{sev:<6}] {d.get('title') or f.stem}")
        return 0
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    lib = load_library()
    require_any_layer(lib)
    st = lib.stats()
    for k, v in st.items():
        print(f"  {k:12} {v}")
    return 0


# ── CLI ────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="exp",
        description="两层经验库(全局 + 项目级)。",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="在当前项目初始化 .exp/")
    s.add_argument("path", nargs="?")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("hook", help="hook 入口,由 Claude Code 调用")
    s.add_argument("event", choices=["session-start", "post-failure",
                                     "user-prompt", "stop"])
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("query", help="按上下文检索经验")
    s.add_argument("context", nargs="+")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(fn=cmd_query)

    s = sub.add_parser("show", help="看一条经验的全文")
    s.add_argument("lesson_id")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("list", help="列出经验")
    s.add_argument("--layer", choices=["project", "global"])
    s.add_argument("--category", choices=CATEGORIES)
    s.add_argument("--status", choices=STATUSES)
    s.add_argument("--health", choices=list(HEALTH_LABEL))
    s.add_argument("--by", default="health", choices=["health", "category"],
                   help="分组方式。默认 health(算出来的,不会腐烂);"
                        "category 是主题,只适合人工翻阅")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("add", help="记一条经验")
    s.add_argument("title")
    s.add_argument("--category", default="其他", choices=CATEGORIES)
    s.add_argument("--status", default="hypothesis", choices=STATUSES)
    s.add_argument("--severity", default="", choices=["", *SEVERITIES])
    s.add_argument("--trigger", default="", help="当你正在做 X 时 —— 检索靠它")
    s.add_argument("--symptom", default="")
    s.add_argument("--root-cause", dest="root_cause", default="")
    s.add_argument("--fix", default="")
    s.add_argument("--evidence", default="")
    s.add_argument("--scope", default="")
    s.add_argument("--layer", default="project", choices=["project", "global"])
    s.add_argument("--signature", action="append", default=[],
                   help="失败签名,可重复。挂上它之后,同样的失败再出现时"
                        "会自动记为复发。签名从 exp distill 的输出里抄。")
    s.add_argument("--from-raw", dest="from_raw", action="append", default=[],
                   help="从 raw 事件里抄一个签名挂上,可重复。"
                        "与 --signature 的区别是**会校验该签名确实出现过** —— "
                        "抄错的签名是个永远不命中的静默坏钩子。")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("distill", help="把原始事件归拢成候选经验")
    s.add_argument("--write", action="store_true",
                   help="生成可编辑的草稿到 .exp/candidates.md")
    s.set_defaults(fn=cmd_distill)

    s = sub.add_parser("feedback", help="给一条经验反馈")
    s.add_argument("lesson_id")
    s.add_argument("--outcome", required=True,
                   choices=["helped", "recurred", "confirmed", "refuted"])
    s.set_defaults(fn=cmd_feedback)

    s = sub.add_parser("gc", help="体检:列出坏经验和死重")
    s.set_defaults(fn=cmd_gc)

    s = sub.add_parser("cluster", help="按触发词聚类(现算,不落盘)")
    s.add_argument("--min-sim", type=float, default=0.28,
                   help="聚成一组的最低相似度")
    s.add_argument("--merge-sim", type=float, default=0.5,
                   help="建议合并的最低相似度")
    s.set_defaults(fn=cmd_cluster)

    s = sub.add_parser("import", help="从旧的 lessons/*.yaml 导入")
    s.add_argument("source")
    s.add_argument("--layer", default="project", choices=["project", "global"])
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("serve", help="开网页看经验库")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("stats", help="统计")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("status", help="安装状态、数据位置、卸载指引")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("migrate", help="把经验文件升级到当前 schema 版本")
    s.add_argument("--apply", action="store_true", help="真正写入(默认 dry-run)")
    s.set_defaults(fn=cmd_migrate)

    s = sub.add_parser("pack", help="经验包:通用型的坑,可共享")
    ps = s.add_subparsers(dest="pack_action", required=True)
    for act, hlp in (("list", "列出可用经验包"),
                     ("show", "看一个经验包含什么"),
                     ("install", "安装经验包")):
        q = ps.add_parser(act, help=hlp)
        q.add_argument("name", nargs="?" if act == "list" else None)
        q.add_argument("--from-dir", dest="from_dir", default="",
                       help="从别处读经验包")
        q.add_argument("--layer", default="global",
                       choices=["project", "global"],
                       help="装到哪一层(默认 global —— 通用型的坑跨项目都用)")
        q.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_pack)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError as e:
        die(f"文件不存在: {e}")
    except Exception as e:
        die(f"{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
