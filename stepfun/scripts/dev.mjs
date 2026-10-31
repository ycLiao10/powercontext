/** 开发模式：同时拉起 Vite dev server 与本地桥接服务 */
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const children = [];

function run(cmd, args, name, color) {
  const child = spawn(cmd, args, { cwd: root, stdio: ["ignore", "pipe", "pipe"], shell: process.platform === "win32" });
  const prefix = `\x1b[${color}m[${name}]\x1b[0m `;
  child.stdout.on("data", (d) => process.stdout.write(prefix + d.toString().replace(/\n(?!$)/g, "\n" + prefix)));
  child.stderr.on("data", (d) => process.stderr.write(prefix + d.toString().replace(/\n(?!$)/g, "\n" + prefix)));
  child.on("exit", (code) => {
    console.log(`${prefix}退出，code=${code}`);
    shutdown(code ?? 0);
  });
  children.push(child);
  return child;
}

function shutdown(code = 0) {
  for (const c of children) {
    if (!c.killed) c.kill();
  }
  process.exit(code);
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));

run("node", ["server/index.mjs"], "api", "36");
run("npx", ["vite"], "web", "35");
