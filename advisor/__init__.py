"""对话式库存顾问业务包。

依赖方向单向向内：
    ui → advisor → {llm, tools, rules, repository, rag, db}
    rules 不 import 任何 LLM / SDK / 网络的东西。

这个方向不是洁癖：rules 是唯一判定点，它要是哪天顺手 import 了 openai，
就再也说不清"没有 API 的时候结论会不会变"。能在没网的环境里单测 rules.py，
是这条约束换来的。
"""

__all__ = [
    "advisor",
    "config",
    "db",
    "llm",
    "onboarding",
    "paths",
    "rag",
    "repository",
    "rules",
    "tools",
    "ui",
]