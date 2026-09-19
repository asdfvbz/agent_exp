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
DEAD_WEIGHT_DAYS = 30    # 创建 N 天、注入 0 次 → 死重

# 索引容量。超过它,平铺 list 就开始漏经验 ——
# 而漏掉的经验永远不会被注入,missed 也永远算不出来。
# 这是"该上聚类了"的可测量信号,而不是一种理念。
INDEX_MAX_ITEMS = 40
INDEX_MAX_CHARS = 4000

# 注入记账的会话窗口(分钟)。失败发生在这段时间内的注入才算"当时在上下文里"。
INJECT_WINDOW_MIN = 90


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
    injected: int = 0
    recurred: int = 0                # 注入了还犯
    missed: int = 0                  # 该注入没注入
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
    def is_dead_weight(self) -> bool:
        """死重:记了从没被检索到,而且已经放了很久。"""
        if self.injected > 0 or not self.created:
            return False
        try:
            age = (dt.date.today() - dt.date.fromisoformat(self.created)).days
        except Exception:
            return False
        return age >= DEAD_WEIGHT_DAYS

    def health(self) -> str:
        if self.status == "needs_rewrite" or self.is_rotten:
            return "rotten"
        if self.is_dead_weight:
            return "dead"
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
            recurred=int(stats.get("recurred") or 0),
            missed=int(stats.get("missed") or 0),
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
            recurred=int(stats.get("recurred") or 0),
            missed=int(stats.get("missed") or 0),
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
                if key in ("injected", "recurred"):
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


def _terms(text: str) -> set:
    """切成检索词。中文用二元组 —— 无需词典,零依赖,
    对「追读率」「风格锁」这类自定义术语够用。
    """
    s = re.sub(r"[^\w一-鿿]+", " ", (text or "").lower())
    words: set = set()
    for chunk in s.split():
        if re.match(r"^[一-鿿]+$", chunk):
            for i in range(len(chunk) - 1):
                words.add(chunk[i:i + 2])
            if len(chunk) == 1:
                words.add(chunk)
        elif len(chunk) > 1:
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
}
HEALTH_ACTION = {
    "rotten": "注入了还是反复犯 —— fix 多半不可执行,重写它。",
    "missed": "该命中却没命中 —— 触发词写偏了,用你实际会想到的词重写。",
    "dead": "记了 30 天从没被检索到 —— 考虑删掉或合并进相近的经验。",
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
        return [l for l in self.all() if sig in l.signatures]

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
            if key in ("injected", "recurred"):
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


def session_id() -> str:
    # Claude Code 会把会话信息放进环境变量;没有就用进程组兜底
    return (os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("CLAUDE_SESSIONID")
            or f"pid{os.getppid()}")


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

    # 优先:hig 严重度 → 复发 → 近期注入
    def rank(l: Lesson):
        return (
            0 if l.severity == "high" else 1,
            0 if l.layer == "project" else 1,
            -l.recurred,
            l.title,
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
        mark = ""
        if l.health() == "rotten":
            mark = " [!]反复复发"
        elif l.is_dead_weight:
            mark = " [--]从未命中"
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
    # 报告出来,你才知道该跑 exp cluster 了。
    dropped = len(items) - len(shown_ids)
    if dropped > 0:
        lines.append(
            f"\n**索引已满({dropped_reason})，有 {dropped} 条未注入。**"
            f"\n这不是正常的 —— 未注入的经验无法被验证。跑 `exp cluster` "
            f"看哪些该合并，或 `exp gc` 清理死重。\n"
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
    """
    lib = Library(find_project_root())
    if not lib.enabled:
        return 0

    layer = lib.project_layer
    if layer is None:
        return 0          # enabled 已经保证了不会是 None,防御性兜底

    if args.event == "session-start":
        return _hook_session_start(lib, layer)
    if args.event == "post-failure":
        return _hook_post_failure(lib, layer)
    if args.event == "stop":
        return _hook_stop(lib, layer)
    return 0


def _save_in_layer(lib: Library, l: Lesson) -> None:
    """只用于 status 等【人决定、要 commit】的字段变更。
    计数器一律走 lib.bump(),不重写文件。
    """
    for layer in lib.layers:
        if layer.name == l.layer:
            layer.save(l)
            return


def _hook_session_start(lib: Library, layer: Layer) -> int:
    block, ids = render_index(lib)
    if not block or not ids:
        return 0
    # level="index":模型看到的只有标题和触发词,没看到 fix。
    # 所以这次的注入只能用于判定 missed,不能用于判定 recurred。
    layer.log_injection(ids, "session-start:index", session_id(), level="index")
    # 一次写盘,不是 N 次 —— 见 Layer.bump_many
    shown = set(ids)
    lib.bump_all((l, "injected", 1) for l in lib.all() if l.id in shown)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": block,
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _hook_post_failure(lib: Library, layer: Layer) -> int:
    """失败发生的那一刻 —— 唯一适合记账的时机。

    这里做三件事,而且【全部无 LLM】:
      1. 算签名
      2. 归因:recurred(注入了还犯) / missed(该注入没注入)
      3. 落原始事件
    """
    payload = _read_stdin_json()
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

    sess = session_id()
    indexed = layer.recent_injections(sess, level="index")
    content = layer.recent_injections(sess, level="content")
    now = _now()
    touched: List[Lesson] = []

    # ── 归因 A:签名命中 ────────────────────────────
    # 这条失败以前【确切见过】。签名是结构化的(路径、数字、引号内容都归一化了),
    # 所以换个措辞的同一种坑也能对上。
    #
    # 判定用 content 而非 index:只有模型【看过做法】之后还犯,
    # 才说明这条经验的 fix 不可执行。只看到标题不算 ——
    # 那多半是它判断这条跟自己无关,那是触发词的问题,记为 missed。
    promote: List[Lesson] = []
    # 所有计数攒起来,最后【一次写盘】—— 逐条 bump 是 N 次全文件读写
    pending: List[Tuple[Lesson, str, int]] = []

    for l in lib.by_signature(sig):
        if l.id in content:
            # 看过做法还是犯 → fix 不可执行
            pending.append((l, "recurred", 1))
            touched.append(l)
            if l.recurred + 1 >= RECUR_THRESHOLD and l.status == "confirmed":
                l.status = "needs_rewrite"
                promote.append(l)
        else:
            pending.append((l, "missed", 1))   # 库里却没到手上 → 触发词写偏了
            touched.append(l)

    # ── 归因 B:没有签名时的兜底 ────────────────────
    #
    # 背景:**签名只能靠 `exp distill` 从原始事件里抄,门槛很高。**
    # 绝大多数经验是没签名的 —— 如果只靠签名归因,那些经验
    # 永远无法被判定"有没有用",`recurred` 就形同虚设。
    #
    # 所以这里用失败信息本身当检索词,按相关度分两种情况:
    #
    #   高度相关 + 注入过   → recurred(看了还是犯,fix 不可执行)
    #   高度相关 + 没注入过 → missed  (该到手上却没到)
    #
    # min_overlap 卡得很高(4 而非默认 2)。归因写错比不写更坏 ——
    # 误判会让人去"修"一条本来没问题的经验。
    if not touched and str(err).strip():
        try:
            near = lib.query(f"{tool} {err}", limit=2, min_overlap=4)
        except Exception:
            near = []
        for l in near:
            if l.id in content:
                # 看过做法还是犯 —— 和归因 A 的判据一致
                pending.append((l, "recurred", 1))
                if l.recurred + 1 >= RECUR_THRESHOLD and l.status == "confirmed":
                    l.status = "needs_rewrite"
                    promote.append(l)
            else:
                pending.append((l, "missed", 1))
            touched.append(l)

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

    # 输出:给模型一句提示。
    #
    # **必须走 stderr + 退出码 2。** PostToolUseFailure 的语义是
    # "exit 2 会把 stderr 送给 Claude";普通 stdout 到不了模型那里。
    # 这里不会阻断任何东西 —— 工具已经失败了,没有可阻断的动作。
    hint = lib.query(f"{tool} {err}", limit=1, min_overlap=3)
    if hint:
        l = hint[0]
        if l.fix:
            print(f"[exp] 这个坑记过:{l.title}\n"
                  f"  做法: {l.fix.splitlines()[0]}", file=sys.stderr)
            return 2
    return 0


def _hook_stop(lib: Library, layer: Layer) -> int:
    """收工时把本次会话的原始事件归拢成候选。

    **不调用 LLM。** 候选整理出来交给 agent 自己归纳 ——
    宿主里那个模型比任何外部调用都更懂当前语境。
    """
    today = dt.date.today().isoformat()
    events = [e for e in layer.raw_events(since=today)
              if e.get("kind") == "failure"
              and e.get("session") == session_id()
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
                  f" (注入{l.injected}/复发{l.recurred}/漏检{l.missed})")
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
    print(f"注入 {l.injected} / 复发 {l.recurred} / 漏检 {l.missed} / 有用 {l.helped}")
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
        order = {"rotten": 0, "missed": 1, "dead": 2, "ok": 3}
        label = {
            "rotten": "反复复发 —— 注入了还是犯,fix 不可执行",
            "missed": "检索漏检 —— 触发词写偏了,该命中没命中",
            "dead":   f"死重 —— {DEAD_WEIGHT_DAYS} 天从没被命中过",
            "ok":     "正常",
        }
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
                flags.append({"rotten": "反复复发", "dead": "死重",
                              "missed": "检索漏检"}[l.health()])
            tail = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {l.severity or '-':6} {l.title}{tail}")
            print(f"         注入{l.injected} 复发{l.recurred} 漏检{l.missed}")
    st = lib.stats()
    print(f"\n合计 {st['total']} 条 "
          f"(项目 {st['project']} / 全局 {st['global']}) · "
          f"确认 {st['confirmed']} · 复发 {st['rotten']} · "
          f"死重 {st['dead']}")
    return 0


# ── 命令:add ───────────────────────────────────────
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
        signatures=list(args.signature or []),
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


def cmd_gc(args: argparse.Namespace) -> int:
    """体检:把坏经验和死重列出来。

    **不自动删除。** 报告,不处置 —— 库的改动应该有人过目。
    """
    lib = load_library()
    st = lib.stats()
    print(f"合计 {st['total']} 条(项目 {st['project']} / 全局 {st['global']})")
    print(f"确认 {st['confirmed']} · 未验证 {st['hypothesis']} · "
          f"已推翻 {st['refuted']}")
    print()

    rotten = [l for l in lib.all() if l.health() == "rotten"]
    missed = [l for l in lib.all() if l.health() == "missed"]
    dead = [l for l in lib.all() if l.is_dead_weight]

    if rotten:
        print(f"── 反复复发 ({len(rotten)}) —— 注入了还是犯,fix 多半不可执行 ──")
        for l in rotten:
            print(f"  {l.title}")
            print(f"    注入{l.injected} 复发{l.recurred} · {l.path}")
        print()
    if missed:
        print(f"── 检索漏检 ({len(missed)}) —— trigger 写偏了,该命中没命中 ──")
        for l in missed:
            print(f"  {l.title}  (漏检{l.missed})")
            print(f"    现在写的触发词: {l.trigger[:70]}")
        print()
    if dead:
        print(f"── 死重 ({len(dead)}) —— 记了 {DEAD_WEIGHT_DAYS} 天从没被命中过 ──")
        for l in dead:
            print(f"  {l.title}  (创建 {l.created})")
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

    if not (rotten or missed or dead or dup or unreadable):
        print("没有需要处理的。")
    else:
        print("处置建议:")
        print("  复发 → 重写 fix,让它可执行;或降级为 hypothesis")
        print("  漏检 → 重写 trigger,用你实际会想到的词")
        print("  死重 → 删掉,或合并进相近的经验")
        print("  重名 → 改 id 或合并")
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
def _index_capacity(lib: Library) -> Dict[str, Any]:
    """索引装得下吗?

    这是【最隐蔽的失效模式】的可视化:索引满了 → 有经验永远不会被注入
    → 它们的 missed 永远算不出来 → 库里显示"一切正常",
    而实际有一批经验从来没机会出现。

    CLI 的 `exp cluster` 会报它,页面也必须报 —— 否则两个界面
    对"库里有没有问题"给出不同答案。
    """
    _, shown = render_index(lib)
    total = len([l for l in lib.all()])
    return {
        "capacity": INDEX_MAX_ITEMS,
        "chars": INDEX_MAX_CHARS,
        "total": total,
        "shown": len(shown),
        "dropped": max(0, total - len(shown)),
    }


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
                "injected": l.injected, "recurred": l.recurred,
                "missed": l.missed, "helped": l.helped,
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
    s.add_argument("event", choices=["session-start", "post-failure", "stop"])
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
    s.add_argument("--health", choices=["ok", "rotten", "dead", "missed"])
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
