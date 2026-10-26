import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { cpSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { createRequire } from "node:module";

// PDF resources follow the locked dependency, rather than a second checked-in copy.
const require = createRequire(import.meta.url);
const pdfRoot = dirname(require.resolve("pdfjs-dist/package.json"));
// CSP（script-src 'self'）下小 JS 若被内联成 data: URI 会被阻断：textfold-engine 外链脚本强制落盘。
const inlineLimit = { include: [/[\\/]textfold-engine[\\/](bridge|after|bootstrap)\.js$/] };
function assetsInlineLimit(filePath: string, _content: Buffer): boolean | undefined {
  if (inlineLimit.include.some(pattern => pattern.test(filePath))) return false;
  return undefined;   // 其余资产走 Vite 默认
}
const resourceFolders = ["wasm", "iccs", "cmaps", "standard_fonts"];
function pdfResources(): Plugin {
  let output = "";
  return {
    name: "literature-pdf-resources",
    configResolved(config) { output = resolve(config.root, config.build.outDir, "pdfjs"); },
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const path = (request.url || "").split("?")[0];
        if (!path.startsWith("/pdfjs/")) return next();
        const relative = decodeURIComponent(path.slice(7));
        const file = resolve(pdfRoot, relative);
        if (!resourceFolders.some(folder => file.startsWith(resolve(pdfRoot, folder) + "/"))) { response.statusCode = 403; response.end(); return; }
        try { response.setHeader("Content-Type", file.endsWith(".wasm") ? "application/wasm" : file.endsWith(".js") ? "text/javascript" : "application/octet-stream"); response.end(readFileSync(file)); }
        catch { response.statusCode = 404; response.end(); }
      });
    },
    closeBundle() {
      mkdirSync(output, { recursive: true });
      for (const folder of resourceFolders) cpSync(join(pdfRoot, folder), join(output, folder), { recursive: true });
    },
  };
}
export default defineConfig({

  plugins: [react(), pdfResources()],
  clearScreen: false,
  server: { strictPort: true, host: "127.0.0.1", port: 1420 },
  envPrefix: ["VITE_", "TAURI_ENV_"],
  build: { target: "safari15", minify: process.env.TAURI_ENV_DEBUG ? false : "esbuild", sourcemap: !!process.env.TAURI_ENV_DEBUG, assetsInlineLimit },
});
