---
description: 把刚踩的坑记成一条经验
argument-hint: [发生了什么]
allowed-tools: Bash, Read
---

把刚才踩的坑记进经验库。

用户描述:$ARGUMENTS

**记之前先判定该不该记。** 只记同时满足这两条的:

1. 这个坑**会再踩**(不是一次性的配置调整)
2. 它**没有天然的归属地**(不在代码注释、不在 README、不在 git 历史里)

不满足就别记 —— 记了只会变成死重。

**然后,触发词是这条经验的全部价值。** 措辞必须是
「**当你正在做 X 时**」,而且要用**用户实际会想到的词**,
不要用抽象概括。

- ✅ `当你准备在配置里写一个数字阈值时`
- ❌ `关于阈值来源的思考`

**`--fix` 必须可执行。**
「注意节奏」不是 fix;「连续两段纯引号之间必须补动作/环境/停顿」
才是。检查方法:照着这句话做,能不能机械化地判断做没做到?

先看库里有没有相近的,有就补充而不是新建:

```bash
python "${CLAUDE_PLUGIN_ROOT}/exp.py" query "<关键词>"
```

然后写:

```bash
python "${CLAUDE_PLUGIN_ROOT}/exp.py" add "<标题>" \
    --category 基架|度量|流程|其他 \
    --trigger "当你正在做 X 时" \
    --symptom "<表现>" \
    --root-cause "<根因>" \
    --fix "<可执行的做法>" \
    --evidence "<实测数据优先于推理>" \
    --severity high|medium|low
```

**如果这个坑有对应的工具报错,顺手挂上签名 —— 价值很大。**
挂了签名的经验,失败时会**自动**把做法送到模型眼前(不用它主动查),
而且能被自动判定"看过做法还是犯"。签名从 `exp distill` 的输出里抄:

```bash
python "${CLAUDE_PLUGIN_ROOT}/exp.py" distill
python "${CLAUDE_PLUGIN_ROOT}/exp.py" add "<标题>" ... --signature "Bash|1|<归一化后的错误>"
```

范围判断:

- 换任何项目都成立(如「YAML 里的星号会毁掉解析」)→ 加 `--layer global`
- 只对当前项目成立 → 默认 `--layer project`

记完把标题和触发词回报给用户,让他确认措辞对不对 ——
**触发词写偏了,这条经验就永远不会被检索到。**
