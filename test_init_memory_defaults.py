"""init-memory 默认 memory_dir/source_cwd 推导回归 (bug fix).
旧默认硬编码 ~/.claude 全局目录 → 读错; 改为 cc_memory_dir(cwd) (与 synthesis-index 一致).
mock bootstrap.init_memory 捕获参数, 不触 LLM/不读真目录。"""
import os
import pathlib
from unittest.mock import patch

import bootstrap
import cli
import projection

FAKE_CWD = "/home/yy/projects/memory-service"
OLD_GLOBAL = str(pathlib.Path.home() / ".claude" / "projects" / "-home-yy--claude" / "memory")

# 1. 无参 → memory_dir = cc_memory_dir(getcwd), source_cwd = getcwd()
with patch.object(bootstrap, "init_memory", return_value={"files": 0}) as m, \
     patch.object(os, "getcwd", return_value=FAKE_CWD):
    cli.init_memory()
mem_arg, kw = m.call_args
assert str(mem_arg[0]) == str(projection.cc_memory_dir(FAKE_CWD)), \
    f"默认 memory_dir 应从 cwd 推导, got {mem_arg[0]}"
assert kw.get("source_cwd") == FAKE_CWD, f"source_cwd 应默认 cwd, got {kw.get('source_cwd')}"
assert str(mem_arg[0]) != OLD_GLOBAL, "回归: 默认 memory_dir 不应是 ~/.claude 全局目录"

# 2. --cwd 显式 → memory_dir 从该 cwd 推, source_cwd = 该 cwd
with patch.object(bootstrap, "init_memory", return_value={"files": 0}) as m:
    cli.init_memory(source_cwd="/some/cwd")
mem_arg, kw = m.call_args
assert str(mem_arg[0]) == str(projection.cc_memory_dir("/some/cwd")), mem_arg[0]
assert kw.get("source_cwd") == "/some/cwd"

# 3. --memory-dir 显式 → 用它 (不覆盖), source_cwd 可独立设
with patch.object(bootstrap, "init_memory", return_value={"files": 0}) as m:
    cli.init_memory(memory_dir="/explicit/dir", source_cwd="/tag/cwd")
mem_arg, kw = m.call_args
assert str(mem_arg[0]) == "/explicit/dir", mem_arg[0]
assert kw.get("source_cwd") == "/tag/cwd"

print("✓ init-memory 默认推导 ok (memory_dir=cc_memory_dir(cwd), source_cwd=cwd, 不读全局)")
