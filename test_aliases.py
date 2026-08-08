"""ADR-D7 别名持久化自验证. db.init(tmp) 隔离, 绝不碰 data/memory.db."""
import tempfile
from pathlib import Path

import db
import store

# db.init(tmp) 隔离
tmpdir = tempfile.mkdtemp()
tmppath = Path(tmpdir) / "mem.db"
db.init(tmppath)  # 强制重建连接到 tmp, 不污染 data/memory.db

# 1. put_entity 存别名 → get_entity 返回 aliases 正确
eid = store.put_entity("New York City", "concept", aliases=["NYC", "Big Apple"])
ent = store.get_entity(eid)
print(f"Test 1 (aliases stored): {ent['name']} aliases={ent['aliases']}")
assert ent["aliases"] == ["NYC", "Big Apple"], f"Expected ['NYC','Big Apple'], got {ent['aliases']}"

# 2. put_entity 存 name_embedding: 显式传向量→存向量; 不传→[](store 不做网络, resolver 拥有 embedding)
eid2 = store.put_entity("Tokyo", "concept", name_embedding=[0.1, 0.2, 0.3])
ent2 = store.get_entity(eid2)
print(f"Test 2a (explicit embedding): {ent2['name']} name_embedding={ent2['name_embedding']}")
assert ent2["name_embedding"] == [0.1, 0.2, 0.3], f"Expected [0.1,0.2,0.3], got {ent2['name_embedding']}"

eid3 = store.put_entity("Osaka", "concept")  # 不传 → 默认 [](纯存储, 无网络)
ent3 = store.get_entity(eid3)
print(f"Test 2b (default empty embedding): {ent3['name']} name_embedding={ent3['name_embedding']}")
assert ent3["name_embedding"] == [], f"Expected [] (store is network-free), got {ent3['name_embedding']}"

# 3. find_entity_exact: 大小写不敏感 name + alias 精确/大小写不敏感 + 未命中 None
#    name="New York City" aliases=["NYC","Big Apple"]
hit_name = store.find_entity_exact("new york city")
assert hit_name is not None, "find_entity_exact('new york city') should hit (case-insensitive name)"
assert hit_name["id"] == eid, f"Expected eid {eid}, got {hit_name['id']}"
print(f"Test 3a (case-insensitive name): find_entity_exact('new york city') → {hit_name['name']}")

hit_alias = store.find_entity_exact("NYC")
assert hit_alias is not None, "find_entity_exact('NYC') should hit (alias exact)"
assert hit_alias["id"] == eid, f"Expected eid {eid}, got {hit_alias['id']}"
print(f"Test 3b (alias exact): find_entity_exact('NYC') → {hit_alias['name']}")

hit_alias_ci = store.find_entity_exact("nyc")
assert hit_alias_ci is not None, "find_entity_exact('nyc') should hit (alias case-insensitive)"
assert hit_alias_ci["id"] == eid, f"Expected eid {eid}, got {hit_alias_ci['id']}"
print(f"Test 3c (alias case-insensitive): find_entity_exact('nyc') → {hit_alias_ci['name']}")

miss = store.find_entity_exact("Boston")
assert miss is None, f"find_entity_exact('Boston') should return None, got {miss}"
print("Test 3d (miss): find_entity_exact('Boston') → None")

# 4. add_aliases 并入去重(保序)
store.add_aliases(eid, ["NYC", "Gotham", "Big Apple"])
ent_after = store.get_entity(eid)
print(f"Test 4 (add_aliases dedup): {ent_after['aliases']}")
assert ent_after["aliases"] == ["NYC", "Big Apple", "Gotham"], (
    f"Expected ['NYC','Big Apple','Gotham'] (dedup, order-preserving), got {ent_after['aliases']}"
)

# 5. 确认不污染 data/memory.db (tmp path 隔离)
assert str(tmppath) in str(db._conn_path), f"Expected conn on tmp, got {db._conn_path}"
print(f"Test 5 (tmp isolation): conn_path={db._conn_path}")

# 清理
import shutil
shutil.rmtree(tmpdir)

print("\n✓ All tests passed")
