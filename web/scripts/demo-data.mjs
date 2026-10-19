/**
 * 一次性：为 README 造一套**合成**演示数据（workspace + 库 + 3 份自造文档），
 * 返回 workspace / kb 的 id，交给 `scripts/screenshot.mjs`（`PKH_WS` / `PKH_KBS`）。
 *
 * 为什么不用真数据截图：库里那 36 个库是个人资料（简历/报名表/聊天记录），
 * 截图会随 README 一起公开发布 ⇒ 只用自造的假文档。
 *
 * ```bash
 * PKH_TOKEN=... node scripts/_demo_data.mjs
 * ```
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "../..");
const API = process.env.PKH_API ?? "http://127.0.0.1:8010/api/v1";
const TOKEN = process.env.PKH_TOKEN;
if (!TOKEN) throw new Error("需要 PKH_TOKEN");

const h = { Authorization: `Bearer ${TOKEN}` };
const j = { ...h, "Content-Type": "application/json" };

async function call(path, init = {}) {
  const res = await fetch(`${API}${path}`, init);
  if (!res.ok) throw new Error(`${path} → ${res.status} ${(await res.text()).slice(0, 200)}`);
  return res.status === 204 ? null : res.json();
}

// 1) 工作区（幂等：同名就复用）
const wsName = "示例：Nimbus 产品知识库";
const wss = await call("/workspaces", { headers: h });
let ws = wss.find((w) => w.name === wsName);
if (!ws) {
  ws = await call("/workspaces", {
    method: "POST",
    headers: j,
    body: JSON.stringify({ name: wsName, description: "README 截图用的合成数据（非真实资料）" }),
  });
  console.log("建工作区", ws.id);
} else console.log("复用工作区", ws.id);

// 2) 库（幂等：同名就复用）
const kbName = "产品文档";
const kbs = await call("/kb", { headers: h });
let kb = kbs.find((k) => k.name === kbName && k.workspace_id === ws.id);
if (!kb) {
  kb = await call("/kb", {
    method: "POST",
    headers: j,
    body: JSON.stringify({
      workspace_id: ws.id,
      name: kbName,
      description: "需求、运维手册与接口说明（合成示例）",
      visibility: "private",
      source_kind: "file",
      chunk_strategy: "recursive",
    }),
  });
  console.log("建知识库", kb.id);
} else console.log("复用知识库", kb.id);

// 3) 上传 3 份自造文档（已 ready 就跳过）
const docs = await call(`/kb/${kb.id}/documents`, { headers: h });
const have = new Set(docs.map((d) => d.file_name));
const dir = resolve(ROOT, "data/imports/readme_demo");
const files = readdirSync(dir).filter((f) => f.endsWith(".md"));
const todo = files.filter((f) => !have.has(f));

if (todo.length) {
  const fd = new FormData();
  for (const f of todo) {
    fd.append("files", new Blob([readFileSync(resolve(dir, f))], { type: "text/markdown" }), f);
  }
  const up = await call(`/kb/${kb.id}/documents`, { method: "POST", headers: h, body: fd });
  console.log("已上传", up.documents.map((d) => d.file_name).join(" / "));
} else console.log("文档已存在，跳过上传");

// 4) 等摄取完成
for (let i = 0; i < 60; i++) {
  const rows = await call(`/kb/${kb.id}/documents`, { headers: h });
  const pending = rows.filter((d) => !["ready", "failed"].includes(d.status));
  if (!pending.length) {
    console.log("摄取结束：", rows.map((d) => `${d.file_name}=${d.status}(${d.chunk_count})`).join(" "));
    break;
  }
  await new Promise((r) => setTimeout(r, 2000));
}

const detail = await call(`/kb/${kb.id}`, { headers: h });
console.log(`\nPKH_WS=${ws.id} PKH_KBS=${kb.id}   （${detail.doc_count} 份文档 / ${detail.chunk_count} 个片段）`);
