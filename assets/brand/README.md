# Literature 品牌图标资产

本目录的图标文件**全部由脚本生成**，不要手改 PNG/ICNS/ICO：

```bash
python3 scripts/build_icons.py            # 重新生成全部资产
python3 scripts/build_icons.py --check    # 只渲染预览到 tmp/icon-preview，不写仓库
```

## 方案（2026-10-17 定稿）

| 方案 | 说明 | 状态 |
| --- | --- | --- |
| `mark`（默认） | 定稿主图：粗体「L」+ 书形底（书脊 / 书页槽 / **波浪书口**）+ 两个圆点，单色黑。**源图是 `assets/brand/monogram.png`**（用户定稿的位图），脚本把它的墨迹提成 alpha 蒙版后放到 macOS squircle 主体上 | **启用**，Tauri 打包用的是这一套（`src-tauri/icons/`） |
| `cards` | 备选：两张圆角卡片对角错位，交叠处是一道 45° 亮蓝斜带（＝原文与译文对齐的位置被点亮）。配色取主题令牌：`#007AFF / #4DA3FF / #0A84FF / 白` | 备选，成套资产在 `assets/brand/cards/` |
| `monogram` | 备选：纯矢量版「L」+ 书形底（直书口）+ 两个圆点 | 备选，成套资产在 `assets/brand/monogram/` |

三套都按 macOS 规范画：主体 squircle 816/1024（四边内缩约 104px、圆角≈22%）。

> `mark` 的墨迹直接来自 `monogram.png`，因此图形与定稿位图逐像素一致；换主图只需替换
> `assets/brand/monogram.png` 再重跑脚本。矢量主图仍保留 `icon.svg`（内嵌位图，供 favicon 用）。

## 资产清单

| 文件 | 用途 |
| --- | --- |
| `icon.svg` | 矢量主图（可手改的源只有 `scripts/build_icons.py` 与 `assets/brand/monogram.png`） |
| `icon.png` | 1024×1024 位图主图 |
| `icon.icns` | macOS（16→1024 全尺寸，含 @2x） |
| `icon.ico` | Windows（16/24/32/48/64/128/256） |
| `32x32.png` / `128x128.png` / `128x128@2x.png` / `256x256.png` / `icon-512.png` | Tauri 与 Linux 常用尺寸 |
| `frontend/public/favicon.*` | 浏览器 favicon（随默认方案一同写入） |

## 换用备选方案

改 `scripts/build_icons.py` 的 `main()`（把 `emit("mark", "src-tauri/icons", ...)` 换成
`emit("cards", ...)` 或 `emit("monogram", ...)`），重跑脚本，再重新打包客户端即可。
几何定义在同一文件顶部常量区，改常量比改图形代码更安全。

## 生效范围

- 仓库内 `src-tauri/icons/` 只影响**下一次打包**；已安装的 `launcher/Literature.app` 图标不会自己更新，
  需要按 `docs/PACKAGING.md` 重新构建安装。
- 浏览器调试入口的 favicon 来自 `frontend/public/`，`pnpm dev` / `pnpm build` 自动带上。
