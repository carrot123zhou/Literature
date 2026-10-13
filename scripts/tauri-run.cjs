// 在 node 可执行文件名不是 `node`（例如临时 adhoc 签名的副本）时运行 Tauri CLI。
// @tauri-apps/cli/tauri.js 依赖 process.argv[0] 的 basename 判断调用方式，名字不匹配
// 时会把 node 路径当成子命令；这里直接调用 native run，绕过该判断。
const path = require("path");
const cli = require(path.join(__dirname, "..", "frontend", "node_modules", "@tauri-apps", "cli", "main.js"));

cli.run(process.argv.slice(2), "tauri")
  .then(() => process.exit(0))
  .catch((error) => {
    cli.logError(error && error.message ? error.message : String(error));
    process.exit(1);
  });
