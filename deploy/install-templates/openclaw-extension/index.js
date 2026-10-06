/**
 * memsvc Plugin
 *
 * T5: before_prompt_build — 每 session 前 3 个 user turn 调 memsvc CLI 召回 KG 注入
 *     (不带 --cwd = 零投影, 设计要求; 全程 fail-open, 失败下轮再试不烧额度)
 * T6: before_compaction — 压缩前把被压缩消息转 CC transcript JSONL 落 transcript-spool
 *     (+ `.jsonl.harness` sidecar 内容 "cc", daemon 据此走 CC 段切管道)
 */

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

const CLI = "/home/yy/projects/memory-service/cli.py";
const SPOOL_DIR = "/home/yy/projects/memory-service/data/transcript-spool";
const OC_ROOT = "/home/yy/.openclaw";
const MAX_INJECTED_TURNS = 3;
const RECALL_TIMEOUT_MS = 8000;
// 短消息门 (首跑实测 2026-10-06: "hi" 类查询零信号还烧 ~2s spawn):
// 规整后 <4 rune 直接跳过 — 不发起召回不计次 (下条实质消息还有全额 3 次)
const MIN_QUERY_CHARS = 4;

const injectedTurns = new Map(); // sessionKey -> 已成功注入次数
let compactionSampled = false; // 首次 before_compaction 打消息形态样本

/** spawn python3 recall, 8s 硬杀; 成功返回 stdout, 失败/超时/exit!=0 返回 null */
function runRecall(query) {
  return new Promise((resolve) => {
    let out = "";
    let settled = false;
    let child;
    try {
      child = spawn("python3", [CLI, "recall", query, "--json", "--vector", "--top-k", "3"], {
        stdio: ["ignore", "pipe", "ignore"],
      });
    } catch {
      resolve(null);
      return;
    }
    const timer = setTimeout(() => child.kill("SIGKILL"), RECALL_TIMEOUT_MS);
    const done = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    child.stdout.on("data", (d) => (out += d));
    child.on("error", () => done(null));
    child.on("close", (code) => done(code === 0 ? out : null));
  });
}

/** manual/budget 压缩路径的事件不带 messages (compaction-hooks.ts:253 只传
 * 计数) — 存储 seam fallback (2026-10-06): 自己读 agent sqlite 的追加式
 * transcript_events (压缩不删事件, 全历史可读)。只读连接, 与 gateway 写入
 * 并发安全 (WAL)。返回 message 数组或 null。 */
function readTranscriptFromStore(agentId, sessionId) {
  if (!agentId || !sessionId) return null;
  const dbPath = path.join(OC_ROOT, "agents", agentId, "agent", "openclaw-agent.sqlite");
  let db;
  try {
    db = new DatabaseSync(dbPath, { readOnly: true });
  } catch {
    return null;
  }
  try {
    const rows = db.prepare(
      "SELECT event_json FROM transcript_events WHERE session_id = ? ORDER BY seq",
    ).all(sessionId);
    const msgs = [];
    for (const r of rows) {
      try {
        const ev = JSON.parse(r.event_json);
        if (ev?.type === "message" && ev?.message?.role) msgs.push(ev.message);
      } catch { /* 坏行跳过 */ }
    }
    return msgs.length ? msgs : null;
  } finally {
    db.close();
  }
}

/** 多形态兼容的文本提取: content string / content[].text / text / parts[].text */
function extractText(m) {
  const c = m?.content;
  if (typeof c === "string") return c;
  const joinTextParts = (arr) =>
    Array.isArray(arr)
      ? arr.filter((p) => p && typeof p.text === "string").map((p) => p.text).join("\n")
      : "";
  if (Array.isArray(c)) return joinTextParts(c);
  if (typeof m?.text === "string") return m.text;
  return joinTextParts(m?.parts);
}

const plugin = {
  id: "memsvc",
  name: "memsvc",
  description: "memsvc KG auto-recall injection (T5) + compaction snapshot to transcript-spool (T6)",
  version: "1.0.0",

  register(api) {
    const log = api.logger;

    // T5: before_prompt_build 自动召回注入 (前 3 个 user turn / session)
    api.on(
      "before_prompt_build",
      async (event, ctx) => {
        if (ctx.trigger !== "user") return undefined;
        const key = ctx.sessionKey || "unknown";
        if ((injectedTurns.get(key) || 0) >= MAX_INJECTED_TURNS) return undefined;
        // 2026-10-06 claw-01 实录: Feishu turn 的 prompt 头部拼有 System: 内部上下文行
        // (维护通知/通道元信息), 原样当 query 会污染召回 — 先剥 System 行再取用户意图
        const query = String(event?.prompt || "")
          .split("\n")
          .filter((l) => !/^System:/.test(l.trim()))
          .join(" ")
          .replace(/<<<[A-Z_]*>>>/g, " ")
          .replace(/\s+/g, " ")
          .trim()
          .slice(0, 240);
        if (query.length < MIN_QUERY_CHARS) return undefined;
        // [System] 前缀 = 内部系统 turn (system-turn-prompt.ts 规范前缀:
        // restart recovery / subagent resume 等续传指令) — 整条无用户意图,
        // 召回必泛命中 (2026-10-06 claw-02 自检实录: query 被 gateway 噪声占满)
        if (query.startsWith("[System]")) return undefined;
        try {
          const stdout = await runRecall(query);
          const facts = stdout ? JSON.parse(stdout)?.facts : null;
          if (!Array.isArray(facts) || facts.length === 0) return undefined;
          // 实测 CLI --json 正文在 fact.value (无 gist/text 字段), fallback 留作形态防御
          const lines = facts
            .slice(0, 3)
            .map((f) =>
              `- ${String(f?.value || f?.text || f?.gist || "")
                .replace(/\s+/g, " ")
                .trim()
                .slice(0, 120)}`,
            )
            .filter((l) => l.length > 2);
          if (lines.length === 0) return undefined;
          injectedTurns.set(key, (injectedTurns.get(key) || 0) + 1);
          if (injectedTurns.size > 500) injectedTurns.clear(); // 软帽防泄漏
          return {
            prependContext: `[memsvc auto-recall | top ${lines.length} | query: ${query.slice(0, 60)}]\n${lines.join("\n")}`,
          };
        } catch (error) {
          log.warn?.(`[memsvc] recall fail-open: ${error instanceof Error ? error.message : String(error)}`);
          return undefined;
        }
      },
      { timeoutMs: 12000 },
    );

    // T6: before_compaction 快照喂 spool
    api.on(
      "before_compaction",
      async (event, ctx) => {
        try {
          let msgs = Array.isArray(event?.messages) ? event.messages : null;
          if ((!msgs || msgs.length === 0)) {
            // manual /compact 与 budget/preflight 闸触发的压缩事件都不带
            // messages (compaction-hooks.ts:253 只传计数; 2026-10-06 实测
            // trigger=manual/budget 两路径) — 存储 Seam fallback 读全量
            // transcript, 段级 sha 幂等管重放 (旧段零重复 LLM)。
            msgs = readTranscriptFromStore(ctx?.agentId, ctx?.sessionId);
            if (msgs) {
              log.info?.(`[memsvc] store fallback: ${msgs.length} msgs (event.messageCount=${event?.messageCount ?? "?"})`);
            }
          }
          if (!msgs || msgs.length === 0) {
            log.warn?.(`[memsvc] before_compaction without messages and store seam empty (agentId=${ctx?.agentId ?? "?"} sessionId=${ctx?.sessionId ?? "?"}) — snapshot skipped`);
            return;
          }

          // 首次调用打真实消息形态样本, 供事后核实
          if (!compactionSampled) {
            compactionSampled = true;
            log.warn(`[memsvc] sample: ${JSON.stringify(msgs.slice(0, 2))}`);
          }

          const lines = [];
          for (const m of msgs) {
            const role = typeof m?.role === "string" ? m.role : "";
            // 只收 user/assistant — system/tool 条目不当 user 原话入蒸馏
            if (role !== "user" && role !== "assistant") continue;
            const text = extractText(m);
            if (!text) continue;
            lines.push(
              JSON.stringify({
                // CC transcript 惯例: user 行 type="user", assistant 行 type="assistant"
                // stop_reason (GB review 2026-10-05): segments_from_snapshot 收段
                // 判据要求 assistant 行 message.stop_reason=="end_turn" — 缺它
                // 0 段 → daemon 判 done 静默删快照零蒸馏
                type: role === "assistant" ? "assistant" : "user",
                message: {
                  role,
                  content: [{ type: "text", text }],
                  ...(role === "assistant" ? { stop_reason: "end_turn" } : {}),
                },
                cwd: ctx?.workspaceDir,
              }),
            );
          }
          if (lines.length === 0) return;

          const sidRaw =
            typeof event.sessionFile === "string" && event.sessionFile
              ? path.basename(event.sessionFile).replace(/\.[^.]+$/, "")
              : String(ctx?.sessionKey || "session");
          const sid = sidRaw.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 64) || "session";
          const payload = `${lines.join("\n")}\n`;
          const sha16 = createHash("sha256").update(payload).digest("hex").slice(0, 16);
          const base = path.join(SPOOL_DIR, `${sid}-${sha16}`);

          // 原子写: tmp + rename, jsonl 正文与 harness sidecar 双文件
          await writeFile(`${base}.jsonl.tmp`, payload, "utf8");
          await rename(`${base}.jsonl.tmp`, `${base}.jsonl`);
          await writeFile(`${base}.jsonl.harness.tmp`, "cc", "utf8");
          await rename(`${base}.jsonl.harness.tmp`, `${base}.jsonl.harness`);

          log.info?.(`[memsvc] compaction snapshot: ${lines.length} lines -> ${path.basename(base)}.jsonl`);
        } catch (error) {
          log.warn?.(`[memsvc] compaction snapshot fail-open: ${error instanceof Error ? error.message : String(error)}`);
        }
      },
      { timeoutMs: 15000 },
    );

    log.info("memsvc plugin registered (T5 auto-recall + T6 compaction snapshot)");
  },
};

export default plugin;
