# -*- coding: utf-8 -*-
"""在【真的没有 PyYAML】的环境下跑全部测试。

**为什么不能只靠"CI 里没 pip install":**
很多 runner(以及不少开发者机器)自带 PyYAML,那样跑的还是
"装了"的那条路径,内置子集的 bug 会一路溜到用户机器上。

真实案例:内置子集的转义还原有 bug(`有"引号"` 读回来变成
`有\\"引号\\"`),而当时的 CI 两条路径都是绿的 —— 因为两条路径
实际上都在用 PyYAML。**"我的环境里没事"正是最不该被容忍的缺陷。**

这里通过劫持 __import__ 真的把 yaml 挡掉,逼代码走 _mini_* 分支。
"""
from __future__ import annotations

import builtins
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_real_import = builtins.__import__


def _blocked(name, *args, **kwargs):
    if name == "yaml" or name.startswith("yaml."):
        raise ImportError("PyYAML 被测试主动屏蔽")
    return _real_import(name, *args, **kwargs)


def main() -> int:
    builtins.__import__ = _blocked
    # 清掉可能已经被导入的 yaml
    for mod in [m for m in sys.modules if m == "yaml" or m.startswith("yaml.")]:
        del sys.modules[mod]

    sys.path.insert(0, str(ROOT / "plugins" / "exp"))
    import exp
    if exp._pyyaml is not None:
        print("[x] 屏蔽失败,yaml 仍被导入 —— 这个脚本没起到作用",
              file=sys.stderr)
        return 2

    print("[i] PyYAML 已屏蔽,走内置 YAML 子集")
    suite = unittest.TestLoader().discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
