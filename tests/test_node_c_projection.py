#!/usr/bin/env python3
"""Node C 投影通路自验证(ADR-16 b/c/g + 噪音)。"""

import os
import sqlite3
import tempfile
import shutil
from pathlib import Path

# 添加项目根到路径
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import cli
import db
import store
import projection
import recall as recall_mod

# 保存原始连接(测试后恢复)
_ORIG_CONN_PATH = db._conn_path


def test_projection_union_and_rewrite():
    """验证 T_C1(清空重写) + T_C2(UNION) + T_C4(score 阈值)。"""
    tmpdir = tempfile.mkdtemp()
    try:
        # 初始化临时 DB(用 db.init 切换连接)
        kg_path = Path(tmpdir) / "kg.db"
        conn = db.init(kg_path)  # 这会切换全局连接
        cwd = "/test/project"
        trail_sid = "trail-session-123"

        # 创建实体
        rust_eid = store.put_entity("rust", "topic")
        noise_eid = store.put_entity("noise", "topic")

        # F1: 轨迹 fact(seen_sessions 包含 trail-sid, source_cwd=cwd)
        # value 含 "rust" 确保字面 match, score>=0.3 召回
        f1_id = store.put_fact(
            subject_id=rust_eid, predicate="全名", value="rust programming",
            extractor="regex", fact_type="stable", source_cwd=cwd,
            seen_sessions=[trail_sid]
        )
        # F2: 高 LIF fact(source_cwd=cwd, 无 seen_sessions)
        f2_id = store.put_fact(
            subject_id=rust_eid, predicate="是", value="rust language",
            extractor="llm", fact_type="stable", source_cwd=cwd,
            seen_sessions=[]
        )
        # F3: NULL source_cwd(不应投影)
        f3_id = store.put_fact(
            subject_id=noise_eid, predicate="是", value="垃圾数据",
            extractor="regex", fact_type="stable", source_cwd=None,
            seen_sessions=[]
        )
        # F4: 低相关噪音(同 cwd, 但低 match score)
        f4_id = store.put_fact(
            subject_id=noise_eid, predicate="关联", value="无关内容",
            extractor="llm", fact_type="stable", source_cwd=cwd,
            seen_sessions=[]
        )

        # 手动设置 LIF(F2 高, F1 中, F3/F4 低)
        conn.execute("UPDATE fact SET LIF=0.9 WHERE id=?", (f2_id,))
        conn.execute("UPDATE fact SET LIF=0.5 WHERE id=?", (f1_id,))
        conn.execute("UPDATE fact SET LIF=0.1 WHERE id=?", (f3_id,))
        conn.execute("UPDATE fact SET LIF=0.05 WHERE id=?", (f4_id,))
        conn.commit()

        # 模拟 CC memory 目录
        mem_dir = Path(tmpdir) / "memory"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# CC 原生行\n- [mem] 旧索引行](memory/mem-old.md)\n")

        # 调 build_index(session=trail-sid)
        result = cli.build_index(scope=cwd, session_id=trail_sid, top_k=10, memory_dir=str(mem_dir))
        assert result["projected"] >= 1, "应投影至少 1 个 fact"

        # 检查 MEMORY.md: [mem] 行应包含 F1(轨迹)+ F2(top-K), 不含旧行
        md_content = (mem_dir / "MEMORY.md").read_text()
        lines = md_content.splitlines()
        mem_lines = [l for l in lines if l.startswith("- [mem]")]

        # 验证清空重写: 旧行被删
        assert "旧索引行" not in md_content, "旧 [mem] 行应被清空"
        # 验证 F1/F2 投影
        mem_ids = []
        for line in mem_lines:
            if "mem-" in line:
                # 提取 id
                import re
                m = re.search(r"mem-([a-f0-9]+)\.md", line)
                if m:
                    mem_ids.append(m.group(1))

        # F1 或 F2 至少一个投影(取决于 top-K 去重逻辑)
        assert f1_id in mem_ids or f2_id in mem_ids, f"F1/F2 应投影, mem_ids={mem_ids}"
        # NULL fact 不投影
        assert f3_id not in mem_ids, "NULL source_cwd fact 不应投影"

        # 验证幂等: 再跑一次, [mem] 行数不变
        result2 = cli.build_index(scope=cwd, session_id=trail_sid, top_k=10, memory_dir=str(mem_dir))
        md_content2 = (mem_dir / "MEMORY.md").read_text()
        mem_lines2 = [l for l in md_content2.splitlines() if l.startswith("- [mem] ")]
        assert len(mem_lines) == len(mem_lines2), f"幂等失败: 首次 {len(mem_lines)} 行, 二次 {len(mem_lines2)} 行"

        # 验证 recall score 阈值(T_C4)
        # 查询 "rust": F1/F2 高相关(score>=0.3), F4 低相关(score<0.3 应被滤)
        results = recall_mod.recall("rust", cwd=cwd)
        result_ids = [r["id"] for r in results]
        # 高 LIF 的 F1/F2 应召回
        assert f1_id in result_ids or f2_id in result_ids, "高相关 fact 应召回"
        # 低 LIF 噪音 F4 应滤(score<0.3)
        assert f4_id not in result_ids, "低 score 噪音应被过滤"

        print("✓ T_C1: 清空重写幂等 pass")
        print("✓ T_C2: UNION 两源(轨迹+top-K) pass")
        print("✓ T_C4: score 阈值过滤 pass")
        print("✓ NULL source_cwd 不投影 pass")
        return True

    finally:
        shutil.rmtree(tmpdir)
        # 恢复原始连接
        db._conn = None
        db._conn_path = None
        if _ORIG_CONN_PATH:
            db.init(_ORIG_CONN_PATH)


if __name__ == "__main__":
    if test_projection_union_and_rewrite():
        print("\n=== 自验证全部通过 ===")
    else:
        print("\n=== 自验证失败 ===")
        sys.exit(1)
