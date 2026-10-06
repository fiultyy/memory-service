#!/usr/bin/env python3
"""json 递归找值/键路径 — list/dict 全形态。

Why: python3 -c 临时探查两次只 walk dict 值、漏 list 里的字符串
(fallbacks 数组), 第二次致 5 个 agent 配置漏改事故。审计一把过。

用法: json-find.py <file.json|-> <needle> [-v]   # needle 在键名或字符串值中(子串)
      -v 同时打印非字符串标量值 (int/float/bool/None)
输出: 每行一条完整路径, 如  agents.entries.x.model.fallbacks[0] = "glm-5.3-flash"
"""
import json
import sys


def walk(node, path, needle, out, show_scalars):
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if needle in str(k):
                out.append(f"{p}  (key)")
            walk(v, p, needle, out, show_scalars)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{path}[{i}]", needle, out, show_scalars)
    elif isinstance(node, str):
        if needle in node:
            out.append(f"{path} = {node!r}")
    elif show_scalars and needle in str(node):
        out.append(f"{path} = {node!r}")


def main(argv):
    show_scalars = "-v" in argv
    args = [a for a in argv[1:] if a != "-v"]
    if len(args) != 2:
        print(__doc__)
        return 2
    src, needle = args
    text = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    out: list[str] = []
    walk(json.loads(text), "", needle, out, show_scalars)
    print("\n".join(out) if out else f"(no match for {needle!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))


def _demo():
    """自检: list 深处的字符串与键必须可达 (dict-only walk 的盲区)。"""
    doc = {"agents": {"entries": {"x": {"model": {
        "primary": "glm-5.3", "fallbacks": ["glm-5.3-flash"]}}}}}
    out: list[str] = []
    walk(doc, "", "5.3-flash", out, False)
    assert out == ["agents.entries.x.model.fallbacks[0] = 'glm-5.3-flash'"], out
    out = []
    walk(doc, "", "fallback", out, False)
    assert out == ["agents.entries.x.model.fallbacks  (key)"], out
    print("json-find self-check ok")
