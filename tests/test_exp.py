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
    def _fail(self, lib, tool, err, code=1):
        """直接调用归因路径,不经过 subprocess。"""
        layer = lib.project_layer
        sig = exp.signature(tool, err, code)
        sess = exp.session_id()
        content = layer.recent_injections(sess, level="content")

        pending, promote = [], []
        for l in lib.by_signature(sig):
            if l.id in content:
                pending.append((l, "recurred", 1))
                if l.recurred + 1 >= exp.RECUR_THRESHOLD and l.status == "confirmed":
                    l.status = "needs_rewrite"
                    promote.append(l)
            else:
                pending.append((l, "missed", 1))
        lib.bump_all(pending)
        for l in promote:
            lib._layers_save(l) if hasattr(lib, "_layers_save") else None
        return sig

    def test_recurred_requires_content_injection(self):
        """看过做法还是犯 → 记为复发。

        只有 content 级注入才算 —— 只看到标题不算,
        那说明模型判断这条跟自己无关,那是触发词的问题。
        """
        les = self.add("会复发的坑", status="confirmed",
                       trigger="当你做某事时", signatures=["Bash|1|boom"])
        lib = self.lib()
        layer = lib.project_layer
        layer.log_injection([les.id], "test", exp.session_id(), level="content")
        exp.Layer.clear_memo()

        lib = self.lib()
        self._fail(lib, "Bash", "boom")
        self.assertEqual(self.lib().get("会复发的坑").recurred, 1)

    def test_missed_when_only_index_injected(self):
        """只注入了索引(没给做法)就犯 → 记为漏检,不是复发。"""
        les = self.add("只见过标题的坑", status="confirmed",
                       trigger="当你做某事时", signatures=["Bash|1|boom"])
        lib = self.lib()
        lib.project_layer.log_injection([les.id], "test", exp.session_id(),
                                        level="index")
        exp.Layer.clear_memo()

        lib = self.lib()
        self._fail(lib, "Bash", "boom")
        got = self.lib().get("只见过标题的坑")
        self.assertEqual(got.missed, 1)
        self.assertEqual(got.recurred, 0)

    def test_auto_demotion_at_threshold(self):
        """复发到阈值 + confirmed → **自动降级** needs_rewrite。"""
        les = self.add("反复复发的坑", status="confirmed",
                       trigger="当你做某事时", signatures=["Bash|1|boom"])
        for _ in range(exp.RECUR_THRESHOLD):
            lib = self.lib()
            lib.project_layer.log_injection([les.id], "t", exp.session_id(),
                                            level="content")
            exp.Layer.clear_memo()
            lib = self.lib()
            pending, promote = [], []
            content = {les.id}
            for l in lib.by_signature("Bash|1|boom"):
                if l.id in content:
                    pending.append((l, "recurred", 1))
                    if l.recurred + 1 >= exp.RECUR_THRESHOLD and l.status == "confirmed":
                        l.status = "needs_rewrite"
                        promote.append(l)
            lib.bump_all(pending)
            for l in promote:
                for lay in lib.layers:
                    if lay.name == l.layer:
                        lay.save(l)
            exp.Layer.clear_memo()

        got = self.lib().get("反复复发的坑")
        self.assertEqual(got.status, "needs_rewrite")
        self.assertEqual(got.health(), "rotten")

    def test_recurred_without_signature(self):
        """**没挂签名也要能判复发。**

        签名只能靠 `exp distill` 从原始事件里抄,门槛很高 ——
        绝大多数经验是没签名的。如果归因只认签名,那些经验
        永远无法被判定"有没有用",`recurred` 就形同虚设。

        判据:失败信息与经验高度相关(重合 ≥4)**且**该经验注入过。
        """
        les = self.add("阈值必须有来源", status="confirmed",
                       trigger="当你准备在配置里写一个数字阈值时",
                       fix="先问这个数哪来的")
        lib = self.lib()
        lib.project_layer.log_injection([les.id], "t", exp.session_id(),
                                        level="content")
        exp.Layer.clear_memo()

        lib = self.lib()
        # 模拟 hook 的归因 B:用失败信息去比对
        content = lib.project_layer.recent_injections(
            exp.session_id(), level="content")
        near = lib.query("Edit 数字阈值必须有来源,不能凭感觉写",
                         limit=2, min_overlap=4)
        self.assertTrue(near, "高度相关的失败应该能命中这条经验")
        pending = []
        for l in near:
            if l.id in content:
                pending.append((l, "recurred", 1))
            else:
                pending.append((l, "missed", 1))
        lib.bump_all(pending)

        got = self.lib().get("阈值必须有来源")
        self.assertEqual(got.recurred, 1, "注入过又犯,应记 recurred")
        self.assertEqual(got.missed, 0)

    def test_missed_without_signature(self):
        """高度相关但从没注入过 → 记 missed。"""
        les = self.add("不要用 var 声明变量", status="confirmed",
                       trigger="当你写 JavaScript 变量声明时",
                       fix="用 const 或 let")
        lib = self.lib()
        content = lib.project_layer.recent_injections(
            exp.session_id(), level="content")
        self.assertNotIn(les.id, content)

        near = lib.query("Write 不要用 var 声明变量", limit=2, min_overlap=4)
        self.assertTrue(near)
        lib.bump_all([(l, "missed", 1) for l in near if l.id not in content])
        self.assertEqual(self.lib().get("不要用 var 声明变量").missed, 1)

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


# ── 健康度 ─────────────────────────────────────────
class TestHealth(Base):
    def test_dead_weight_after_threshold_days(self):
        old = ("2020-01-01")
        self.add("很久没命中的经验", created=old)
        self.assertTrue(self.lib().get("很久没命中的经验").is_dead_weight)

    def test_fresh_lesson_is_not_dead(self):
        import datetime as dt
        self.add("新经验", created=dt.date.today().isoformat())
        self.assertFalse(self.lib().get("新经验").is_dead_weight)

    def test_injected_lesson_never_dead(self):
        """被命中过就不算死重,不管多老。"""
        les = self.add("老但被命中过", created="2020-01-01")
        self.lib().bump_all([(les, "injected", 1)])
        self.assertFalse(self.lib().get("老但被命中过").is_dead_weight)

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
    def test_truncation_reported_not_silent(self):
        """**截断必须报告。**

        静默截断是最隐蔽的失效:被漏掉的经验永远不会被注入,
        所以它们的 missed 永远算不出来 —— 库里显示"一切正常"。
        """
        for i in range(exp.INDEX_MAX_ITEMS + 5):
            self.add(f"经验{i:03d}", trigger=f"当你处理第{i}类问题时")
        lib = self.lib()
        block, shown = exp.render_index(lib)
        self.assertEqual(len(shown), exp.INDEX_MAX_ITEMS)
        self.assertIn("未注入", block, "截断必须在索引里明确说明")

    def test_no_warning_when_fits(self):
        self.add("唯一的一条", trigger="当你测试时")
        block, shown = exp.render_index(self.lib())
        self.assertEqual(len(shown), 1)
        self.assertNotIn("未注入", block)


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
