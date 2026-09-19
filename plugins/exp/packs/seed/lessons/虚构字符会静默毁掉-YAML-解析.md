---
id: 虚构字符会静默毁掉-YAML-解析
title: 虚构字符会静默毁掉 YAML 解析,而报错指向别处
category: 基架
status: confirmed
severity: high
trigger: 当你要写或编辑任何 YAML 文件时;配置文件、CI 配置、k8s manifest、OpenAPI 描述
related: []
signatures: []
created: '2026-09-19'
---

## 表现

写进 YAML 值里的 markdown 强调符号(星号)或英文引号,会让解析器把它们当成 YAML 语法本身(锚点别名、字符串边界),文件直接坏掉。

**最坏的地方是报错位置。** 一个字符的错误会让**所有**读该文件的工具同时报错,
排查时你看到的是四五个不相关的报错,指向完全不同的地方。

## 根因

把 YAML 当成了"纯文本格式",而不是"有语法的格式"。
写内容时脑子里想的是"我要强调这个词",手上敲的是 markdown 方言 ——
而 YAML 里星号是**锚点引用**语法。

## 做法

1. YAML 里**不用** markdown 强调符号。要强调用中文引号「」或全角书名号。
2. 字符串里有引号、冒号、井号时,**加引号包住整段**。
3. **写完立刻验证**,别等 CI:

```bash
python -c "import yaml,sys; yaml.safe_load(open(sys.argv[1],encoding='utf-8')); print('OK')" <文件>
```

4. 程序化写 YAML 时,**写之前先序列化再回读校验** —— 解析不出来就不许落盘。

## 证据

同一个错误在一个真实项目里犯过三次。第三次之后加了写盘前的回读校验,
再没发生过。

## 适用范围

所有手写或程序化生成 YAML 的场景。
