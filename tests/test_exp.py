# -*- coding: utf-8 -*-
"""exp 的回归测试。

**为什么这些测试特别重要:**

这个系统最擅长的失效模式是「看起来一切正常」—— 归因逻辑坏掉之后,
界面仍然显示得好好的,只是再也不会有经验被标记为坏的。
所以核心行为必须有断言保护,不能靠肉眼。

运行:
    python -m unittest discover -s tests -v
    python tests/test_exp.py
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "exp"))

import exp  # noqa: E402


class Base(unittest.TestCase):
    """每个用例一个干净的项目 + 隔离的全局层。

    隔离全局层是必须的:否则测试会往开发者的真实 ~/.exp/ 里写东西。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()
        self.expdir = self.proj / ".exp"
        self.expdir.mkdir()
        exp.Layer.clear_memo()

        # 会话身份必须由 hook payload 提供。清掉环境变量,免得开发机上
        # 真设了 CLAUDE_SESSION_ID 时测试"碰巧通过" —— 那正是上一版
        # 漏掉 session_id bug 的原因:测试替真实环境把变量补上了。
        # CLAUDE_CODE_SESSION_ID 是**权威名字**(2.1.132+ 才加),
        # 也必须清 —— 否则在真实会话里跑测试时,开发机的变量会漏进
        # 被测代码,让某些用例"碰巧通过"。这正是上一版漏掉
        # session_id bug 的机制,不能再犯第二次。
        for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                  "CLAUDE_SESSIONID"):
            os.environ.pop(k, None)

        # 把全局层指到临时目录 —— 绝不碰真实的 ~/.exp/
        self._old_global = exp.GLOBAL_DIR
        exp.GLOBAL_DIR = Path(self.tmp.name) / "global"
        self.addCleanup(self._restore_global)

    def _restore_global(self):
        exp.GLOBAL_DIR = self._old_global
        exp.Layer.clear_memo()

    def lib(self) -> exp.Library:
        exp.Layer.clear_memo()
        return exp.Library(self.expdir)

    def layer(self) -> exp.Layer:
        l = exp.Layer(self.expdir, "project")
        l.ensure()
        return l

    # ── 真实 hook 入口 ─────────────────────────────
    #
    # **归因相关的测试一律走这里,不要另抄一份逻辑。**
    # 上一版在 TestAttribution 里手抄了归因分支,于是测的是抄本 ——
    # 真正的 hook 路径从没被覆盖,`session_id()` 那个致命 bug
    # 因此藏了很久。这些 helper 放在 Base 上,谁都能用。
    def hook(self, event, payload):
        """模拟 CLI 调用 hook:真读 stdin,真走 cmd_hook。

        返回 (exit_code, stdout, stderr) —— 退出码本身是契约的一部分
        (post-failure 靠 exit 2 把做法送给模型)。
        """
        class _Stdin:
            def __init__(self, data):
                self.buffer = io.BytesIO(data)

            def read(self):
                return self.buffer.read().decode("utf-8")

        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(sys, "stdin", _Stdin(raw)), \
                unittest.mock.patch.object(sys, "stdout", out), \
                unittest.mock.patch.object(sys, "stderr", err), \
                unittest.mock.patch.object(exp, "find_project_root",
                                           lambda start=None: self.expdir):
            code = exp.cmd_hook(argparse.Namespace(event=event))
        exp.Layer.clear_memo()
        return code, out.getvalue(), err.getvalue()

    def start(self, session="S1"):
        """跑一次 SessionStart hook。"""
        return self.hook("session-start", {"session_id": session})

    def fail(self, session="S1", tool="Bash", err="boom", code="1"):
        """跑一次 PostToolUseFailure hook。"""
        return self.hook("post-failure", {
            "session_id": session, "tool_name": tool,
            "error": err, "exit_code": code,
        })

    def lesson(self, title):
        """重读一条经验(绕过缓存),拿到最新计数。"""
        self.lib()
        return self.lib().get(title)

    def add(self, title, **kw):
        l = self.layer()
        les = exp.Lesson(
            id=exp._slug(title), layer="project", title=title,
            category=kw.get("category", "其他"),
            status=kw.get("status", "hypothesis"),
            severity=kw.get("severity", ""),
            trigger=kw.get("trigger", ""),
            fix=kw.get("fix", ""), root_cause=kw.get("root_cause", ""),
            signatures=list(kw.get("signatures", [])),
            created=kw.get("created", "2026-01-01"),
        )
        l.save(les)
        exp.Layer.clear_memo()
        return les


# ── 序列化 ─────────────────────────────────────────
class TestSerialization(Base):
    def test_roundtrip(self):
        """写出去的文件必须能原样读回来。"""
        self.add("一条经验", trigger="当你测试时", fix="这样做",
                 severity="high", status="confirmed")
        l = self.lib().get("一条经验")
        self.assertIsNotNone(l)
        self.assertEqual(l.trigger, "当你测试时")
        self.assertEqual(l.fix, "这样做")
        self.assertEqual(l.severity, "high")
        self.assertEqual(l.status, "confirmed")

    def test_schema_version_written(self):
        """**每个文件都必须带 schema 版本。**

        没有它,插件升级时无法判断文件是哪个格式写的 ——
        只能靠猜,而猜错 = 弄坏用户数据。
        """
        les = self.add("带版本的经验")
        text = les.path.read_text(encoding="utf-8")
        self.assertIn("schema:", text)
        self.assertEqual(self.lib().get("带版本的经验").schema, exp.SCHEMA_VERSION)

    def test_yaml_hostile_content_rejected_or_escaped(self):
        """含 YAML 敌意字符的标题不能把文件写坏。

        这条是踩过坑的:星号 / 英文引号进 YAML 会静默毁掉解析,
        而报错会指向四个不相关的地方。
        """
        weird = '标题里有 "引号" 和: 冒号'
        self.add(weird, trigger="当你测试时")
        # 写盘后必须仍可解析,且 id 对得上
        path = self.layer().lessons_dir / f"{exp._slug(weird)}.md"
        self.assertTrue(path.exists(), "文件应已写入")
        fm, _ = exp._split_frontmatter(path.read_text(encoding="utf-8"))
        self.assertTrue(fm.strip(), "frontmatter 不应为空")
        self.assertTrue(exp._load_yaml(fm), "frontmatter 应可解析")

    def test_hostile_content_survives_roundtrip_unchanged(self):
        """**值必须原样回来,不只是"能解析"。**

        上一版只断言了"可解析",于是内置 YAML 子集的转义 bug
        溜过去了:写出去时把 `"` 转成 `\\"`,读回来不还原,
        结果 `有"引号"` 静默变成 `有\\"引号\\"`。

        这个 bug **只在没装 PyYAML 的机器上出现** —— 最不该被
        容忍的正是这种"我的环境里没事"的缺陷。
        """
        cases = [
            '有 "引号" 的标题',
            "有 '单引号' 的",
            "有: 冒号 的",
            "有 # 井号 的",
            "混合 \"双\" 和 '单' 还有: 冒号",
            "反斜杠 C:\\path\\to\\file",
        ]
        for text in cases:
            with self.subTest(text=text):
                les = self.add(text, trigger="当你测试往返时")
                got = self.lib().get(exp._slug(text))
                self.assertIsNotNone(got, f"写出去读不回来: {text!r}")
                self.assertEqual(got.title, text, "标题被静默改坏了")

    def test_yaml_subset_roundtrip_directly(self):
        """直接测内置 YAML 子集的往返(不经过文件)。

        无论当前环境装没装 PyYAML,子集实现本身都必须是对的 ——
        它是"零依赖"承诺的底线。
        """
        cases = [
            {"id": "a", "title": '有"引号"'},
            {"id": "b", "title": "有: 冒号"},
            {"id": "c", "title": "反斜杠 \\x\\y"},
            {"id": "d", "related": ["x", "y"],
             "stats": {"injected": 5, "last_injected": ""}},
        ]
        for src in cases:
            with self.subTest(src=src):
                back = exp._mini_load(exp._mini_dump(src))
                self.assertEqual(back, src)

    def test_counters_not_in_lesson_file(self):
        """**计数器绝不写进经验文件。**

        经验要 commit;计数器高频变。混在一起 = 团队天天冲突,
        而且冲突发生在人类写的正文旁边。
        """
        les = self.add("计数器不该进文件")
        l = self.lib()
        l.bump_all([(les, "injected", 5)])
        text = les.path.read_text(encoding="utf-8")
        self.assertNotIn("injected", text)
        self.assertNotIn("recurred", text)
        # 但读的时候要能拿到
        self.assertEqual(self.lib().get("计数器不该进文件").injected, 5)


# ── 检索 ───────────────────────────────────────────
class TestQuery(Base):
    def test_matches_by_trigger(self):
        self.add("YAML 星号会毁掉解析",
                 trigger="当你要写或编辑任何 YAML 文件时", severity="high")
        self.add("无关的经验", trigger="当你做完全无关的事情时")
        hits = self.lib().query("我要写 YAML 配置文件")
        self.assertTrue(hits)
        self.assertEqual(hits[0].id, "YAML-星号会毁掉解析")

    def test_refuted_excluded(self):
        self.add("被推翻的经验", trigger="当你写 YAML 配置时",
                 status="refuted")
        self.assertEqual(self.lib().query("我要写 YAML 配置"), [])

    def test_category_not_in_match_pool(self):
        """**category 不能进检索匹配池** —— 它是导出量,不是事实。

        它需要"其他"桶,所以一定会腐烂;让它参与打分只会稀释
        真正有效的 trigger。
        """
        self.add("无关注释经验", category="度量",
                 trigger="当你做完全无关的事情时")
        # 只提分类名,不该命中
        self.assertEqual(self.lib().query("度量"), [])

    def test_min_overlap_blocks_false_positive(self):
        """重合度不够时宁可漏报 —— 错误的经验比没有经验更坏。"""
        self.add("某条经验", trigger="当你处理数据库迁移时")
        self.assertEqual(self.lib().query("数据库", min_overlap=4), [])


# ── 归因(项目核心)─────────────────────────────────
class TestAttribution(Base):
    """归因 —— 项目核心。

    **这些用例走真实入口 `cmd_hook`,绝不重抄一份归因逻辑。**

    上一版这里有个 `_fail()`,把生产代码的归因分支原样抄了一遍,
    于是测的是抄本而非实现 —— 真正的 hook 路径从来没被覆盖过。
    后果是 `session_id()` 那个致命 bug 藏了很久:

        hook 命令经过一层 shell,每次调用的 getppid() 都不同
        → 第 2 次失败去另一个 pid 名下找注入记录 → 永远找不到
        → `recurred` 恒为 0,所有签名命中都被误记成 missed

    而且旧测试手工调 `log_injection(..., exp.session_id())`、
    又在开发机上碰巧有 CLAUDE_SESSION_ID,等于替真实环境把变量补上了。
    所以:**从 stdin 进,从计数器出。**
    """

    # ── 地基:会话身份 ──────────────────────────────

    def test_session_id_comes_from_payload(self):
        """身份以 payload 为准 —— 它是每个 hook 事件都带的权威来源。"""
        self.assertEqual(exp.session_id({"session_id": "abc"}), "abc")

    def test_session_id_never_fabricates_from_pid(self):
        """**拿不到会话时返回空,不能编一个 pid。**

        这是那个 bug 的核心:pid{getppid()} 每次都变,于是两次失败
        永远对不上号,而症状是静默的 —— recurred 恒为 0,
        表面看只是"还没有坏经验"。宁可显式返回空。
        """
        sid = exp.session_id({})
        self.assertEqual(sid, "", "没有会话信息时不该造一个 id 出来")
        self.assertNotIn("pid", sid)

    def test_same_session_spans_separate_hook_processes(self):
        """注入和归因发生在**两次独立的进程**里,必须能对上号。

        这条用例如果不走真实入口就永远测不出来 —— 正是上一版的问题。
        """
        les = self.add("会复发的坑", status="confirmed", trigger="当你做某事时",
                       fix="这样做", signatures=["Bash|1|boom"])
        self.start("S1")                    # 进程 A:注入索引
        self.fail("S1")                     # 进程 B:失败 → 得能看见 A 的注入

        # 第一次失败:骨架精确命中但正文还没送过 → 饿死,不是漏检
        got = self.lesson("会复发的坑")
        self.assertEqual(got.starved, 1)
        self.assertEqual(got.missed, 0, "签名是精确匹配的,不该怪 trigger")
        self.assertEqual(got.recurred, 0)

    # ── 闭环:全自动,不依赖模型自觉 ─────────────────

    def test_closed_loop_without_any_voluntary_action(self):
        """**核心承诺**:三件事不依赖任何人的自觉。

        模型从头到尾没主动跑过 exp query / exp show,只靠 hook,
        recurred 也必须能自己长出来。
        """
        self.add("会复发的坑", status="confirmed", trigger="当你做某事时",
                 fix="这样做", signatures=["Bash|1|boom"])
        self.start("S1")

        code, _, err = self.fail("S1")
        self.assertEqual(code, 2, "第一次失败就该把做法送给模型")
        self.assertIn("这样做", err)

        self.fail("S1")                     # 又犯一次
        got = self.lesson("会复发的坑")
        self.assertEqual(got.recurred, 1,
                         "做法已送达又再犯 → 复发,全自动闭合")
        self.assertEqual(got.missed, 0)

    def test_hint_delivery_is_recorded(self):
        """投递了就要记账 —— 否则"送过"和"没送过"分不清,recurred 永远算不出来。"""
        self.add("会复发的坑", trigger="当你做某事时", fix="这样做",
                 signatures=["Bash|1|boom"])
        self.start("S1")
        self.fail("S1")

        inj = (self.expdir / "injections.jsonl").read_text(encoding="utf-8")
        self.assertIn("post-failure:hint", inj)
        self.assertIn("S1", inj)

    def test_auto_demotion_at_threshold(self):
        """复发到阈值 + confirmed → **自动降级** needs_rewrite。全自动。"""
        self.add("反复复发的坑", status="confirmed", trigger="当你做某事时",
                 fix="这样做", signatures=["Bash|1|boom"])
        self.start("S1")
        for _ in range(exp.RECUR_THRESHOLD + 1):
            self.fail("S1")

        got = self.lesson("反复复发的坑")
        self.assertEqual(got.status, "needs_rewrite")
        self.assertEqual(got.health(), "rotten")

    # ── 归因的四个格子 ─────────────────────────────

    def test_skeleton_hit_never_records_missed(self):
        """**骨架精确命中的岔子只可能是投递问题,不是 trigger 问题。**

        这条守着 gc 的诊断正确性:签名撞上了是硬证据,这条 trigger
        一个字都不用改。如果记成 missed,gc 会建议"重写触发词",
        用户就会去改一个完全没问题的字段 —— 误诊比不诊断更坏。
        """
        self.add("阈值必须有来源", status="confirmed",
                 trigger="当你准备在配置里写一个数字阈值时",
                 signatures=["Bash|1|boom"])          # 注意:不给 fix
        self.start("S1")
        self.fail("S1")
        self.fail("S1")

        got = self.lesson("阈值必须有来源")
        self.assertGreater(got.starved, 0)
        self.assertEqual(got.missed, 0)
        self.assertEqual(got.health(), "starved")

    def test_recurred_requires_content_not_just_index(self):
        """只看到标题不算看过做法 —— 那是触发词的问题,不是 fix 的问题。"""
        les = self.add("只见过标题的坑", status="confirmed",
                       trigger="当你做某事时", signatures=["Bash|1|boom"])
        lib = self.lib()
        lib.project_layer.log_injection([les.id], "manual", "S1", level="index")
        exp.Layer.clear_memo()

        self.fail("S1")
        got = self.lesson("只见过标题的坑")
        self.assertEqual(got.recurred, 0, "只看过标题不算看过做法")
        self.assertEqual(got.starved, 1, "骨架命中 + 没送正文 → 投递饿死")

    def test_missed_without_signature(self):
        """没挂签名 + 语义没命中 → 这才是真的 missed(trigger 该改)。"""
        self.add("不要用 var 声明变量", status="confirmed",
                 trigger="当你写 JavaScript 变量声明时",
                 fix="用 const 或 let")
        self.start("S1")
        # 失败信息与这条经验高度相关,但它是靠语义匹配上的
        self.fail("S1", tool="Write", err="不要用 var 声明变量", code="")

        got = self.lesson("不要用 var 声明变量")
        self.assertEqual(got.missed, 1)
        self.assertEqual(got.starved, 0, "没签名就不该走骨架路径")

    def test_recurred_without_signature(self):
        """**没挂签名也要能判复发。**

        签名只能靠 `exp distill` 从原始事件里抄,门槛很高 ——
        绝大多数经验是没签名的。只认签名的话,它们的 recurred 形同虚设。
        """
        les = self.add("不要用 var 声明变量", status="confirmed",
                       trigger="当你写 JavaScript 变量声明时", fix="用 const")
        lib = self.lib()
        lib.project_layer.log_injection([les.id], "manual", "S1",
                                        level="content")
        exp.Layer.clear_memo()

        self.fail("S1", tool="Write", err="不要用 var 声明变量", code="")
        got = self.lesson("不要用 var 声明变量")
        self.assertEqual(got.recurred, 1, "看过做法又犯 → 复发")
        self.assertEqual(got.missed, 0)

    def test_unknown_session_does_not_blame_trigger(self):
        """会话身份缺失时,骨架命中仍不该被记成 missed。

        宁可少一个信号(starved 仍是有用信号),也不能把不确定
        变成一条会误导人的诊断。
        """
        self.add("会复发的坑", trigger="当你做某事时",
                 signatures=["Bash|1|boom"])
        # 不带 session_id 的 payload
        self.hook("post-failure", {"tool_name": "Bash", "error": "boom",
                                   "exit_code": "1"})
        got = self.lesson("会复发的坑")
        self.assertEqual(got.missed, 0)
        self.assertEqual(got.starved, 1)
        self.assertEqual(got.recurred, 0, "身份未知不能断言看过还犯")

    def test_lesson_without_signature_and_unrelated_failure_is_untouched(self):
        """不相关的失败不该动任何计数 —— 归因宁可漏,不可错。"""
        self.add("某条经验", trigger="当你处理数据库迁移时")
        self.start("S1")
        self.fail("S1", err="unrelated stack trace here")
        got = self.lesson("某条经验")
        self.assertEqual((got.recurred, got.missed, got.starved), (0, 0, 0))

    def test_low_overlap_does_not_attribute(self):
        """相关度不够时**不能**归因 —— 误判比不判更坏。"""
        self.add("某条经验", trigger="当你处理数据库迁移时")
        lib = self.lib()
        self.assertEqual(
            lib.query("Bash exit code 1", limit=2, min_overlap=4), [],
            "无关的失败不该匹配到任何经验")

    def test_signature_normalizes_paths_and_numbers(self):
        """签名要按结构算,不是原文 —— 否则换个措辞就当成新坑。"""
        a = exp.signature("Bash", "Error at /home/x/a.py line 42")
        b = exp.signature("Bash", "Error at /var/log/b.py line 99")
        self.assertEqual(a, b, "路径和行号应被归一化掉")


class TestRepeatHint(Base):
    """**库里没有对应经验时,重复失败必须被说出来。**

    这是投递路径上最大的漏洞,而它此前【在任何测试里都没有覆盖】——
    这正是它藏住的原因。

    漏洞的形状:签名在失败发生的那一刻就算出来了,而且落进了 raw/,
    但匹配只发生在 `by_signature(经验)` 上,而那条路径要求经验
    先挂上签名。于是第 2 次踩坑和第 1 次完全同形:exit 0、什么都不送。
    系统手里握着"你犯过这个错"的硬证据,却保持沉默。

    这里所有用例都走真实 hook 入口(`self.fail`)——
    跟 TestAttribution 一样的理由:抄一份逻辑来测等于没测。
    """

    def setUp(self):
        super().setUp()
        # **必须先建出 lessons/。** 一个空的 .exp/ 不算激活
        # (见 TestLayers.test_empty_expdir_not_enabled)—— 不建的话
        # cmd_hook 会静默返回 0,一条 raw 都记不下来,而这些用例
        # 恰恰是在测"raw 里记了几次"。症状是全都返回 0,看起来像
        # 提示逻辑没生效。
        self.layer()

    def test_first_occurrence_is_silent(self):
        """第 1 次出现不提示 —— 那时无从判断它会不会重复,说什么都是噪声。"""
        self.start("S1")
        code, _, err = self.fail("S1", err="boom one")
        self.assertEqual(code, 0, "第 1 次不该说话")
        self.assertEqual(err, "")

    def test_second_occurrence_speaks(self):
        """第 2 次是「重复」被确证的那一刻 —— 必须说话。"""
        self.start("S1")
        self.fail("S1", err="boom one")
        code, _, err = self.fail("S1", err="boom one")

        self.assertEqual(code, 2, "第 2 次必须送到模型眼前")
        self.assertIn("第 2 次", err)
        self.assertIn("库里没有对应经验", err)
        self.assertIn("--from-raw", err, "提示必须给出可执行的下一步")

    def test_hint_carries_tool_and_raw_error(self):
        """签名里的 <STR> 是归一化过的 —— 必须带上原文,否则模型认不出是哪条报错。"""
        self.start("S1")
        self.fail("S1", tool="Bash", err='psql: ERROR: relation "users" does not exist')
        _, _, err = self.fail("S1", tool="Bash",
                              err='psql: ERROR: relation "users" does not exist')
        self.assertIn('relation "users" does not exist', err)
        self.assertIn("Bash", err)

    def test_hint_is_said_only_once(self):
        """第 3、4 次再报就是纯噪声 —— 模型已经知道了。"""
        self.start("S1")
        self.fail("S1", err="boom one")
        self.fail("S1", err="boom one")          # 第 2 次:说话
        code, _, err = self.fail("S1", err="boom one")   # 第 3 次:闭嘴
        self.assertEqual(code, 0)
        self.assertNotIn("第 3 次", err)

    def test_signature_lesson_wins_over_repeat_hint(self):
        """有对应经验时走 fix 投递,不该走重复提示 —— 两条路不叠。"""
        self.add("会复发的坑", trigger="当你做某事时", fix="这样做",
                 signatures=["Bash|1|boom"])
        self.start("S1")
        self.fail("S1", err="boom")              # 有经验:送 fix
        code, _, err = self.fail("S1", err="boom")
        self.assertIn("这个坑记过", err)
        self.assertNotIn("库里没有对应经验", err)

    def test_unsignable_failure_is_silent(self):
        """算不出签名的失败不参与 —— 没有签名就没法判断是不是重复。"""
        self.start("S1")
        for _ in range(3):
            code, _, err = self.fail("S1", tool="", err="", code="")
            self.assertEqual(code, 0)
            self.assertEqual(err, "")


class TestPreActionDelivery(Base):
    """**任务前投递 —— "按需加载"的那个"按需"。**

    这是唯一能真正在【动作之前】把经验送进上下文的通道:

      SessionStart      全量索引(标题+触发词)  → 知道库存在
      UserPromptSubmit  命中条目的【全文】      → 现在就能用
      PostToolUseFailure 失败时送 fix           → 已经晚了

    PreToolUse 看着更合适(知道要跑什么),但它的 additionalContext
    是和工具结果【同一次】送达的 —— 模型看到经验时,命令已经跑完了。
    这是 Messages API 的结构决定的,不是实现选择。所以这里的用例
    打的是 UserPromptSubmit 这个真实入口。
    """

    def setUp(self):
        super().setUp()
        self.layer()

    def prompt(self, text, session="S1"):
        return self.hook("user-prompt", {"session_id": session,
                                         "prompt": text})

    def test_relevant_lesson_is_delivered_before_acting(self):
        """相关时,把【正文】送到 —— 不是标题,是能照做的做法。"""
        self.add("阈值必须有来源", status="confirmed",
                 trigger="当你准备写一个数字阈值时",
                 fix="先找到这个数字的来源:实测、SLA、还是行业惯例。找不到就先别写。")
        code, out, _ = self.prompt("我要设一个超时阈值,该填多少")

        self.assertEqual(code, 0)
        payload = json.loads(out)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("阈值必须有来源", ctx)
        self.assertIn("先找到这个数字的来源", ctx, "必须给做法,不是只给标题")
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"],
                         "UserPromptSubmit")

    def test_irrelevant_prompt_stays_silent(self):
        """不相关时【什么都不注入】—— 噪声会留在会话历史里,代价是持续的。"""
        self.add("阈值必须有来源", trigger="当你准备写一个数字阈值时",
                 fix="先找到来源")
        code, out, _ = self.prompt("帮我把 README 的错别字改一下")
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "不该注入任何东西")

    def test_says_nothing_when_library_is_empty(self):
        """空库不报错、不注入 —— 冷启动期的正常状态。"""
        code, out, _ = self.prompt("帮我改个配置")
        self.assertEqual((code, out), (0, ""))

    def test_lesson_without_fix_is_not_delivered(self):
        """没写做法的经验不推 —— 模型知道了也做不了什么,白占上下文。"""
        self.add("某条没写做法的经验", trigger="当你准备写一个数字阈值时")
        code, out, _ = self.prompt("我要设一个超时阈值")
        self.assertEqual(out, "")

    def test_rotten_lesson_is_not_delivered(self):
        """反复复发过的经验不能当做法推 —— 它的 fix 已知不可执行。

        索引里会标 [反复复发] 让模型自己判断,但**正文投递不行**:
        正文投递的语气是"照这个做"。
        """
        self.add("坏掉的经验", status="confirmed",
                 trigger="当你准备写一个数字阈值时", fix="这个做法没用")
        les = self.lesson("坏掉的经验")
        les.recurred = 99
        self.layer().save(les)
        exp.Layer.clear_memo()
        code, out, _ = self.prompt("我要设一个超时阈值")
        self.assertEqual(out, "", "坏经验不该被当成做法推出去")

    def test_not_delivered_twice_in_same_session(self):
        """**同一条经验在一个会话里只推一次。**

        additionalContext 会留在会话历史里 —— 反复注入同一条不是提醒,
        是纯噪声,而且每一轮都在烧上下文。
        """
        self.add("阈值必须有来源", status="confirmed",
                 trigger="当你准备写一个数字阈值时", fix="先找到来源")
        _, out1, _ = self.prompt("我要设一个超时阈值")
        self.assertIn("阈值必须有来源", out1)

        _, out2, _ = self.prompt("再帮我设一个重试次数阈值")
        self.assertNotIn("阈值必须有来源", out2, "同一会话不重复推")

    def test_delivered_again_in_a_new_session(self):
        """换了会话就该重推 —— 上次那个会话的历史已经不在了。"""
        self.add("阈值必须有来源", status="confirmed",
                 trigger="当你准备写一个数字阈值时", fix="先找到来源")
        _, out1, _ = self.prompt("我要设一个超时阈值", session="S1")
        self.assertIn("阈值必须有来源", out1)
        _, out2, _ = self.prompt("我要设一个超时阈值", session="S2")
        self.assertIn("阈值必须有来源", out2, "新会话应重新投递")

    def test_delivery_is_recorded_as_content_level(self):
        """**记账必须是 content 级,而且必须记。**

        记成 index 会让 recurred 永远算不出来;不记则这次投递
        在数据上不存在 —— 而"送过做法还是犯"正是要量的东西。
        这条投递让 recurred 有了更严格的含义:
        任务【开始前】就给过做法了,你还是踩了。
        """
        self.add("阈值必须有来源", status="confirmed",
                 trigger="当你准备写一个数字阈值时", fix="先找到来源")
        self.prompt("我要设一个超时阈值")

        inj = self.layer().recent_injections("S1", level="content")
        self.assertIn("阈值必须有来源", inj)
        got = self.lesson("阈值必须有来源")
        self.assertEqual(got.injected, 1)

    def test_empty_prompt_does_nothing(self):
        """空 prompt 不参与 —— 没有上下文可检索。"""
        self.add("阈值必须有来源", trigger="当你准备写一个数字阈值时",
                 fix="先找到来源")
        code, out, _ = self.prompt("   ")
        self.assertEqual((code, out), (0, ""))


class TestFromRaw(Base):
    """`exp add --from-raw` —— 把 raw 里的签名接过来,替掉手抄。

    签名是唯一确定性的匹配依据,却只能从 `exp distill` 的输出里手抄,
    门槛高到大多数经验干脆不挂。这一步去掉手抄,但**必须校验** ——
    抄错的签名是个永远不命中的静默坏钩子。
    """

    def setUp(self):
        super().setUp()
        self.layer()        # 空的 .exp/ 不算激活,必须先建出 lessons/
        # raw 里先有真实失败记录
        self.start("S1")
        self.fail("S1", err='psql: ERROR: relation "users" does not exist')

    def _add(self, **kw):
        ns = argparse.Namespace(
            title=kw.get("title", "改了 schema 没同步派生文件"),
            category="其他", status="hypothesis", severity="",
            trigger=kw.get("trigger", "当你修改数据库 schema 时"),
            symptom="", root_cause="", fix=kw.get("fix", "改完跑 make gen"),
            evidence="", scope="", layer="project",
            signature=kw.get("signature", []),
            from_raw=kw.get("from_raw", []),
        )
        # find_project_root 返回的是 **.exp/ 本身**(不是项目根),
        # 所以这里给 self.expdir。
        #
        # lambda 必须能吃任意实参:load_library 里是 `find_project_root()`
        # 无参调用,而 Base.hook 里是 `find_project_root(start)`。
        # 签名写死成 `lambda start=None` 会在其中之一上抛 TypeError ——
        # 而 load_library 把它包在 try/except 里,mock 的报错会被
        # 当成"找不到项目"静默吞掉,最后报出来的是完全无关的错。
        with unittest.mock.patch.object(exp, "find_project_root",
                                        lambda *a, **k: self.expdir):
            return exp.cmd_add(ns)

    def test_from_raw_attaches_the_real_signature(self):
        sig = exp.signature("Bash", 'psql: ERROR: relation "users" does not exist',
                            "1")
        self._add(from_raw=[sig])
        got = self.lesson("改了 schema 没同步派生文件")
        self.assertIn(sig, got.signatures)

    def test_wrong_signature_is_rejected_not_silently_kept(self):
        """**抄错的签名必须当场报错。**

        静默收下它就等于造了个永远不命中的钩子:失败时 by_signature
        找不到,归因和投递都当这条经验不存在,而库里显示一切正常 ——
        跟 missed 是同一种失效模式。
        """
        with self.assertRaises(SystemExit):
            self._add(from_raw=["Bash|1|这个签名根本不存在"])

    def test_from_raw_signature_actually_matches_later_failure(self):
        """端到端:抄来的签名必须真的能在后续失败时命中。"""
        sig = exp.signature("Bash", 'psql: ERROR: relation "users" does not exist',
                            "1")
        self._add(from_raw=[sig])
        code, _, err = self.fail("S1", tool="Bash",
                                 err='psql: ERROR: relation "users" does not exist')
        self.assertEqual(code, 2, "挂了签名之后这次必须送到模型眼前")
        self.assertIn("这个坑记过", err)


# ── 健康度 ─────────────────────────────────────────
class TestHealth(Base):
    """健康度分组。**每一组的处置方向不同,分错比不分更坏。**

    尤其是 starved_index 和 dead:现象完全一样(注入 0 次),
    病因和处方却相反 ——
        死重      进过索引但没人读 → 删掉,或改 trigger
        索引饿死  连索引都没进过   → 扩容量,**不许删**
    当成同一类处理,会让用户删掉一批只是没排上队的有用经验。
    """

    def test_excluded_is_starved_index_not_dead(self):
        """被索引挤掉过 + 从没进去过 → 索引饿死,不是死重。

        **判据是被挤掉的次数,不是年龄。** 容量溢出是算术事实:
        50 条经验、上限 40 条,今天就排除了 10 条,跟放了多久无关。
        拿年龄当判据会让信号晚 30 天出现,而这段时间里
        用户看到的一切正常 —— 那正是本项目最想消灭的失效模式。
        """
        les = self.add("被容量挤掉的", created="2020-01-01")
        self.lib().bump_all([(les, "excluded", 3)])

        got = self.lib().get("被容量挤掉的")
        self.assertTrue(got.is_starved_index)
        self.assertFalse(got.is_dead_weight, "它根本没机会露面,不叫死重")
        self.assertEqual(got.health(), "starved_index")

    def test_exclusion_reported_immediately_not_after_30_days(self):
        """**容量问题是当天就报的,不用等 30 天。**

        死重需要时间(这条经验可能只是还没被用上),但索引装不下
        是结构事实 —— 一条今天刚写、今天就排在 41 位的经验,
        今天就已经没机会了。
        """
        import datetime as dt
        les = self.add("今天刚写的", created=dt.date.today().isoformat())
        self.lib().bump_all([(les, "excluded", 1)])

        got = self.lib().get("今天刚写的")
        self.assertEqual(got.health(), "starved_index",
                         "新建的经验被挤掉,当场就该报出来")

    def test_indexed_but_never_read_is_dead(self):
        """进过索引却没被拉过全文 → 这才是死重。"""
        les = self.add("进过索引没人读", created="2020-01-01")
        self.lib().bump_all([(les, "indexed", 3)])
        got = self.lib().get("进过索引没人读")
        self.assertTrue(got.is_dead_weight)
        self.assertFalse(got.is_starved_index)
        self.assertEqual(got.health(), "dead")

    def test_indexed_beats_excluded(self):
        """进过索引的,就算也被挤掉过,也不算饿死 —— 它有过机会。"""
        les = self.add("进去过也被挤过", created="2020-01-01")
        self.lib().bump_all([(les, "indexed", 1), (les, "excluded", 5)])
        got = self.lib().get("进去过也被挤过")
        self.assertFalse(got.is_starved_index)
        self.assertTrue(got.is_dead_weight, "有过机会却没被读 → 死重")

    def test_fresh_lesson_is_neither(self):
        import datetime as dt
        self.add("新经验", created=dt.date.today().isoformat())
        got = self.lib().get("新经验")
        self.assertFalse(got.is_dead_weight)
        self.assertFalse(got.is_starved_index)

    def test_injected_lesson_never_dead(self):
        """被投递过就不算死重,不管多老。"""
        les = self.add("老但被命中过", created="2020-01-01")
        self.lib().bump_all([(les, "indexed", 5), (les, "injected", 1)])
        got = self.lib().get("老但被命中过")
        self.assertFalse(got.is_dead_weight)
        self.assertFalse(got.is_starved_index)

    def test_session_start_counts_as_indexed_not_injected(self):
        """索引和正文是两件事,必须分开记 —— 否则死重/饿死分不开。"""
        les = self.add("只进索引", trigger="当你做某事时")
        self.start("S1")

        got = self.lib().get("只进索引")
        self.assertEqual(got.indexed, 1)
        self.assertEqual(got.excluded, 0)
        self.assertEqual(got.injected, 0,
                         "只有标题进了上下文,不能算正文投递过")
        self.assertEqual(les.id, got.id)

    def test_overflow_is_recorded_as_excluded_at_render_time(self):
        """**没挤进索引的必须当场记账** —— 这是"被容量挤掉"的唯一确凿证据。

        只记"谁进去了"的话,被挤掉的那些和"从没被渲染过"的
        在数据上完全一样,于是只能靠年龄猜 —— 而容量问题是
        算术事实,当天就该报。
        """
        n = 130
        for i in range(n):
            self.add(f"经验{i:03d}", trigger=f"当你处理第{i}类问题时")
        self.start("S1")

        items = self.lib().all()
        shown = [l for l in items if l.indexed > 0]
        dropped = [l for l in items if l.excluded > 0]
        self.assertEqual(len(shown) + len(dropped), n, "每条都该被记一笔")
        self.assertTrue(dropped, "超过容量就该有人被挤掉")
        self.assertTrue(all(l.health() == "starved_index" for l in dropped))
        # 它们是**当天**就报出来的,不是等 30 天
        self.assertTrue(all(l.created for l in dropped))

    def test_starved_ranks_ahead_of_fresh_in_index(self):
        """**打破饿死的自我锁定。**

        被容量挤掉的经验 indexed 恒为 0,而排序又把它压在最后 ——
        越饿死越靠后,越靠后越饿死。所以 starved 要提到前面:
        一条反复撞上却从没送到的经验,比一条刚写完还没人踩过的
        更该占那 40 个格子之一。
        """
        import datetime as dt
        starved = self.add("饿死过的", trigger="当你做 A 时")
        fresh = self.add("新写的", trigger="当你做 B 时",
                         created=dt.date.today().isoformat())
        self.lib().bump_all([(starved, "starved", exp.STARVE_THRESHOLD)])

        block, ids = exp.render_index(self.lib())
        self.assertIn(starved.id, ids)
        self.assertLess(ids.index(starved.id), ids.index(fresh.id),
                        "饿死过的应该排在前面")
        self.assertIn("饿死", block)

    def test_health_priority(self):
        """rotten 优先于 dead —— 反复复发比从没命中更值得处理。"""
        les = self.add("又老又复发", created="2020-01-01", status="confirmed")
        for _ in range(exp.RECUR_THRESHOLD):
            les.recurred += 1
        self.assertEqual(les.health(), "rotten")


# ── 缓存 ───────────────────────────────────────────
class TestCache(Base):
    def test_cache_invalidated_on_file_change(self):
        """改文件后必须重新解析 —— 缓存失效写错 = 用户改了经验却看不到。"""
        les = self.add("原名")
        self.assertEqual(self.lib().get("原名").title, "原名")

        p = les.path
        p.write_text(p.read_text(encoding="utf-8").replace("title: 原名",
                                                          "title: 改名后"),
                     encoding="utf-8")
        exp.Layer.clear_memo()
        self.assertEqual(self.lib().get(exp._slug("原名")).title, "改名后")

    def test_cache_isolated_per_layer(self):
        """不同目录的层不能共用缓存。"""
        self.add("经验A")
        other = Path(self.tmp.name) / "other" / ".exp"
        l2 = exp.Layer(other, "project")
        l2.ensure()
        l2.save(exp.Lesson(id="经验B", layer="project", title="经验B",
                           created="2026-01-01"))
        exp.Layer.clear_memo()
        self.assertIsNone(self.lib().get("经验B"))


# ── 索引容量 ───────────────────────────────────────
class TestIndexCapacity(Base):
    def test_fills_by_char_budget_not_item_count(self):
        """**真正约束成本的是字符预算,不是条数。**

        上一版 INDEX_MAX_ITEMS=40,而 4000 字符预算实测能装约 78 条 ——
        一半预算白白浪费,10 条经验被无谓地挤掉。
        现在条数只是防爆上限,装不下就装不下,别提前截断。
        """
        for i in range(exp.INDEX_MAX_ITEMS + 5):
            self.add(f"经验{i:03d}", trigger=f"当你处理第{i}类问题时")
        _, shown = exp.render_index(self.lib())
        self.assertGreater(len(shown), exp.INDEX_MAX_ITEMS,
                           "预算还有富余就不该按条数提前截断")
        self.assertLess(len(shown), exp.INDEX_MAX_ITEMS + 5)

    def test_truncation_reported_not_silent(self):
        """**截断必须报告,而且要告诉模型怎么够到剩下的。**

        静默截断是最隐蔽的失效:被漏掉的经验永远不会被注入,
        所以它们的 missed 永远算不出来 —— 库里显示"一切正常"。
        """
        n = 200
        for i in range(n):
            self.add(f"经验{i:03d}", trigger=f"当你处理第{i}类问题时")
        block, shown = exp.render_index(self.lib())
        self.assertLess(len(shown), n, "确实发生了截断")
        self.assertIn(f"库里共 {n} 条", block, "必须说明库比列表大")

    def test_truncation_tells_model_how_to_reach_the_rest(self):
        """**截断提示要对模型有用,不是给维护者看的。**

        上一版写的是"跑 exp cluster 看哪些该合并" —— 读这段文字的是模型,
        它既不跑 cluster 也不合并经验,只会把这句当噪声。
        对模型有用的只有:库比列表大,以及用 exp query 能拿到剩下的。
        """
        for i in range(200):
            self.add(f"经验{i:03d}", trigger=f"当你处理第{i}类问题时")
        block, _ = exp.render_index(self.lib())
        self.assertIn("exp query", block,
                      "要告诉模型怎么够到没列出的经验")

    def test_no_report_when_fits(self):
        self.add("唯一的一条", trigger="当你测试时")
        block, shown = exp.render_index(self.lib())
        self.assertEqual(len(shown), 1)
        self.assertNotIn("库里共", block)


# ── 数据完整性 ─────────────────────────────────────
class TestDataIntegrity(Base):
    """这些是"不崩溃但悄悄失灵"的问题。

    它们比崩溃更难发现:界面看起来完全正常,只是数据已经不对了。
    """

    def test_stats_recovered_from_backup(self):
        """stats.json 损坏时从 .bak 恢复,而不是静默归零。

        直接回空会让"文件被写坏"和"全新安装"变得无法区分 ——
        所有计数静默归零,等于把所有经验重新变回"没被验证过"。
        """
        les = self.add("重要经验")
        lib = self.lib()
        lib.bump_all([(les, "helped", 2)])

        self.assertTrue((self.expdir / "stats.json.bak").exists(),
                        "写入时应留一份备份")

        (self.expdir / "stats.json").write_text("{ 坏掉的 json",
                                                encoding="utf-8")
        exp.Layer.clear_memo()
        got = exp.Layer(self.expdir, "project").load_stats()
        self.assertEqual(got.get("重要经验", {}).get("helped"), 2,
                         "损坏时应从备份恢复")

    def test_stats_survives_both_corrupt(self):
        """两份都坏时回空,而不是抛异常。"""
        (self.expdir / "stats.json").write_text("x", encoding="utf-8")
        (self.expdir / "stats.json.bak").write_text("y", encoding="utf-8")
        self.assertEqual(
            exp.Layer(self.expdir, "project").load_stats(), {})

    def test_duplicate_ids_detected(self):
        """id 重复会让按 id 取经验失效 —— 必须能被检出。

        `get()` 因为歧义返回 None,但 `list` 显示得好好的;
        更糟的是归因按 id 记账,两条会争抢同一份计数。
        """
        d = self.layer().lessons_dir
        for name, title in (("a.md", "第一条"), ("b.md", "第二条")):
            (d / name).write_text(
                f"---\nschema: 1\nid: 重名\ntitle: {title}\n"
                f"created: 2026-01-01\n---\n\n## 做法\n\nx\n",
                encoding="utf-8")
        exp.Layer.clear_memo()
        dup = exp.Layer(self.expdir, "project").duplicate_ids()
        self.assertIn("重名", dup)
        self.assertEqual(len(dup["重名"]), 2)

    def test_no_duplicates_reported_when_clean(self):
        self.add("甲", trigger="当你测试甲时")
        self.add("乙", trigger="当你测试乙时")
        exp.Layer.clear_memo()
        self.assertEqual(
            exp.Layer(self.expdir, "project").duplicate_ids(), {})

    def test_unparseable_file_detected(self):
        """没有 frontmatter 的文件会被当成空经验,应该能报出来。"""
        (self.layer().lessons_dir / "plain.md").write_text(
            "就是一段普通文本\n", encoding="utf-8")
        exp.Layer.clear_memo()
        lay = exp.Layer(self.expdir, "project")
        bad = []
        for p in lay.paths():
            fm, _ = exp._split_frontmatter(
                p.read_text(encoding="utf-8", errors="replace"))
            if not fm.strip() or not exp._load_yaml(fm):
                bad.append(p)
        self.assertTrue(any("plain" in str(p) for p in bad))


# ── 畸形输入 ───────────────────────────────────────
class TestMalformedInput(Base):
    """hook 的 stdin 什么都可能收到,而且**永远不能抛异常**。

    这条路径挂在每一次工具失败上。它崩了 = 归因静默失效,
    而表现是"坏经验怎么一直没被标出来" —— 极难定位。
    """

    def test_scrub_removes_surrogates(self):
        """代理项不能编码成 UTF-8,必须被清掉。

        真实场景:Windows 上按码页解码管道输入会产生 `\\udc80`
        这类字符,随后 json.dumps + write 抛 UnicodeEncodeError,
        整个 post-failure 崩掉。
        """
        bad = "数字阈值必须有来源\udc80\udc81"
        cleaned = exp._scrub(bad)
        self.assertNotIn("\udc80", cleaned)
        cleaned.encode("utf-8")          # 不该抛
        self.assertIn("数字阈值", cleaned)   # 正常部分要保住

    def test_scrub_passes_normal_text(self):
        for s in ("普通中文", "emoji 🎯", "mixed 中英 text", ""):
            self.assertEqual(exp._scrub(s), s)

    def test_append_raw_survives_surrogate(self):
        """含代理项的记录也要能落盘,不能把 hook 打断。"""
        lay = self.layer()
        lay.append_raw({"ts": "2026-01-01T00:00:00", "kind": "failure",
                        "error": "坏了\udc80", "signature": "X|1|坏了\udc80"})
        events = lay.raw_events()
        self.assertEqual(len(events), 1)
        self.assertIn("坏了", events[0]["error"])

    def test_log_injection_survives_surrogate(self):
        lay = self.layer()
        lay.log_injection(["某条经验"], "上下文\udc80", "sess", level="content")
        self.assertEqual(lay.recent_injections("sess"), {"某条经验"})

    def test_read_stdin_json_never_raises(self):
        """各种垃圾输入都要返回 dict,不能抛。"""
        import io
        for junk in ("", "   ", "not json", "{", '{"a":', "\udc80",
                     '{"tool_name": "Bash"}', '[1,2,3]', "null"):
            with self.subTest(junk=repr(junk)):
                old = sys.stdin
                try:
                    sys.stdin = type("S", (), {
                        "buffer": io.BytesIO(junk.encode("utf-8", "replace")),
                        "read": lambda self=None: junk,
                    })()
                    got = exp._read_stdin_json()
                    self.assertIsInstance(got, dict)
                finally:
                    sys.stdin = old

    def test_signature_survives_surrogate(self):
        sig = exp.signature("Bash", "错误\udc80发生", 1)
        sig.encode("utf-8")              # 不该抛
        self.assertIsInstance(sig, str)


# ── 并发 ───────────────────────────────────────────
class TestConcurrency(Base):
    """计数不能丢。

    这个系统最擅长制造"看起来一切正常"的失效 —— 计数少记几次
    和根本没记,在诊断上是同一件事:你查不出哪条经验是坏的。
    所以并发写入必须有断言保护。
    """

    def test_concurrent_writes_do_not_lose_updates(self):
        les = self.add("并发目标", trigger="当并发时")
        n = 16

        def worker(_i):
            lay = exp.Layer(self.expdir, "project")
            lay.bump_many([(les.id, "helped", 1)])

        import threading
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        got = exp.Layer(self.expdir, "project").load_stats().get(
            les.id, {}).get("helped", 0)
        self.assertEqual(got, n, f"并发 {n} 次写入丢了 {n - got} 次")

    def test_lock_released_after_write(self):
        """锁必须被释放 —— 加锁的死锁比不加锁的丢数据更糟。"""
        les = self.add("锁释放测试")
        lay = exp.Layer(self.expdir, "project")
        lay.bump_many([(les.id, "injected", 1)])
        self.assertFalse((self.expdir / ".stats.lock").exists(),
                         "写完之后锁文件应该没了")

    def test_stale_lock_recovered(self):
        """持锁进程崩了留下的陈旧锁要能自动恢复,不能永久卡住。"""
        import time as _t
        les = self.add("陈旧锁测试")
        lock = self.expdir / ".stats.lock"
        lock.write_text("", encoding="utf-8")
        # 把 mtime 改到很久以前,模拟残留
        old = _t.time() - 60
        os.utime(lock, (old, old))

        lay = exp.Layer(self.expdir, "project")
        lay.bump_many([(les.id, "injected", 1)])   # 不该卡住
        self.assertEqual(
            exp.Layer(self.expdir, "project").load_stats()
            .get(les.id, {}).get("injected", 0), 1)


# ── 两层 ───────────────────────────────────────────
class TestLayers(Base):
    def test_global_found_from_subdirectory(self):
        """从子目录启动也要能找到 .exp/ —— 向上查找。"""
        (self.proj / "a" / "b").mkdir(parents=True)
        found = exp.find_project_root(self.proj / "a" / "b")
        # 两边都 resolve —— Windows 上短名(ADMINI~1)和长名会不等
        self.assertEqual(found.resolve(), self.expdir.resolve())

    def test_not_enabled_without_expdir(self):
        """没有 .exp/ 就不是激活状态 —— 插件全局装,机制按项目生效。"""
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertFalse(exp.Library(empty, include_global=False).enabled)

    def test_empty_expdir_not_enabled(self):
        """空的 .exp/(用户手建的)不该激活 —— 必须 init 过。"""
        empty = Path(self.tmp.name) / "handmade" / ".exp"
        empty.mkdir(parents=True)
        exp.Layer.clear_memo()
        self.assertFalse(exp.Library(empty, include_global=False).enabled)

    def test_global_dir_not_mistaken_for_project(self):
        """**全局层不能被当成项目层。**

        全局层在 ~/.exp/,它自己就是一个 .exp/ 目录。向上遍历会在
        用户主目录撞上它,于是每个项目都凭空多出一层指向同一目录的
        假项目层 —— 经验被算两遍、注入两遍、计数记两遍。

        注意:必须 patch 掉 GLOBAL_DIR。测试的临时目录是
        C:/Users/<u>/AppData/Local/Temp/... —— 父目录链正好穿过
        真实主目录,不隔离的话测的是开发机的 ~/.exp/。
        """
        fake_home = Path(self.tmp.name) / "home"
        gdir = fake_home / exp.PROJECT_DIRNAME
        (gdir / "lessons").mkdir(parents=True)
        deep = fake_home / "work" / "proj"
        deep.mkdir(parents=True)

        with unittest.mock.patch.object(exp, "GLOBAL_DIR", gdir):
            exp.Layer.clear_memo()
            found = exp.find_project_root(deep)
            # 断言的是【真正的性质】:那个全局层不能被当成项目层。
            # 不能断言 `is None` —— 测试的临时目录在
            # C:/Users/<u>/AppData/Local/Temp 下,父目录链会穿过真实主目录,
            # 开发机上若存在 ~/.exp/ 就会被找到(那是正确的行为)。
            self.assertFalse(exp._same_path(found, gdir),
                             f"全局层被误当成项目层: {found}")
            exp.Layer.clear_memo()

    def test_project_layer_found_alongside_global(self):
        """有真项目层时仍要能找到它,且两层不重复。"""
        fake_home = Path(self.tmp.name) / "home2"
        gdir = fake_home / exp.PROJECT_DIRNAME
        (gdir / "lessons").mkdir(parents=True)
        proj = fake_home / "work"
        (proj / exp.PROJECT_DIRNAME / "lessons").mkdir(parents=True)

        with unittest.mock.patch.object(exp, "GLOBAL_DIR", gdir):
            exp.Layer.clear_memo()
            found = exp.find_project_root(proj)
            self.assertIsNotNone(found)
            self.assertEqual(found.resolve(),
                             (proj / exp.PROJECT_DIRNAME).resolve())

            lib = exp.Library(found)
            roots = [str(l.root.resolve()) for l in lib.layers]
            self.assertEqual(len(roots), len(set(roots)),
                             f"层目录重复了: {roots}")
            exp.Layer.clear_memo()


class TestDegradation(Base):
    """没有项目层时,该能用的命令要能用。

    用户不会永远按你设想的方式使用 —— 全局层的卖点就是
    "跨项目跟着人走",而没 init 过的项目恰恰最需要它。
    """

    def test_global_usable_without_project_layer(self):
        """没有项目层时,全局层仍然可读写。"""
        gdir = Path(self.tmp.name) / "home3" / exp.PROJECT_DIRNAME
        (gdir / "lessons").mkdir(parents=True)
        with unittest.mock.patch.object(exp, "GLOBAL_DIR", gdir):
            exp.Layer.clear_memo()
            lib = exp.Library(None, include_global=True)   # 只有全局层
            self.assertFalse(lib.enabled)
            self.assertIsNone(lib.project_layer)
            self.assertIsNotNone(lib.global_layer)

            gl = exp.Layer(gdir, "global")
            gl.save(exp.Lesson(id="全局经验", layer="global",
                               title="全局经验", created="2026-01-01"))
            exp.Layer.clear_memo()

            lib2 = exp.Library(None, include_global=True)
            self.assertEqual(len(lib2.all()), 1)
            self.assertEqual(lib2.get("全局经验").title, "全局经验")
            exp.Layer.clear_memo()


if __name__ == "__main__":
    unittest.main(verbosity=2)
