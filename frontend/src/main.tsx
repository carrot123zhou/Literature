/* 载入顺序即层叠优先级 —— 这里刻意让 CSS 先于 App 的子模块执行。
 *
 * 历史问题：原写法把 `import App` 放在 CSS 之前，导致 App 子树里的
 * resource.css / review.css / reading.css / export.css 先于 styles.css、
 * mineru.css 求值，于是 mineru.css 反向覆盖了较新的界面样式。
 * 现在由本文件统一决定顺序（组件内的重复 import 会被 bundler 去重）：
 *
 *   1 styles        旧全局基线
 *   2 mineru        旧 MinerU/首页/设置 大表（历史遗留，逐步被下列文件覆盖）
 *   3 resource      文件资源库
 *   4 review        结构校对
 *   5 reading       对照翻译（+ 5b reader 阅读模块，同层）
 *   6 export        导出
 *   7 theme         ★ 唯一令牌层（必须在 mineru 之后，才能覆盖其 :root 令牌）
 *   8 base          ★ 全局重置 + 组件基元
 *   9 shell         ★ 全局外壳覆盖层（ui-shell）
 *  10 intake        ★ 首页导入 / 解析历史 / 文件设置（ui-intake）
 *  11 app-settings  ★ 设置 / 模型服务 / 任务面板（ui-shell）
 *
 * 新增覆盖一律写进 9–11 对应文件，不要再往 styles.css / mineru.css 追加。
 */
import "./styles.css";
import "./mineru.css";
import "./resource.css";
import "./review/review.css";
import "./workbench/reading.css";
import "./reader/reader.css";
import "./workbench/export.css";
import "./theme.css";
import "./base.css";
import "./shell.css";
import "./intake.css";
import "./app-settings.css";
import "./workbench/parse-engine.css";

import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
