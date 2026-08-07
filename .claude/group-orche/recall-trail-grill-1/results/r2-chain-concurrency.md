# Round 2 终稿:.last_session_id 并发安全

## 【查证】
- 并发覆盖:多 CC session 各自 UserPromptSubmit 写同一 .last_session_id → 后写覆盖先写,错配 session。频率高(每用户消息一次)。
- 生命周期:无清理机制,session 结束残留旧值,下次启动指向已过期 session。
- 文件语义:单次 write 原子(≤PIPE_BUF),但多 writer 无锁;flock 只保写入安全,不解决"哪个是当前 session"。

## 【结论】文件方案本质缺陷(不可行)
核心矛盾:文件是全局单值,CLI 是独立进程**无法知道自己在哪个 session** → 循环依赖(session_id 需从 session 写入,但 CLI 不知当前 session)。加锁(flock+PID)只治标,最后写入者仍非"当前 session"。

## 【修订方案】否决文件方案
已被 chain-alt-mechanism 的 env 方案完全取代(env 直接给 CLI 当前 session_id,无需文件)。本角度结论:文件路径死路,env 是正解。

(原推演的 per-session 文件/CLI 自动发现最新 trail/用户显式 --session 均不如 env 直接。)
