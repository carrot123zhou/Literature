import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

/**
 * pdfjs-dist 5.7 在 OptionalContentConfig 里调用较新的 TC39 提案方法
 * `Map/WeakMap.prototype.getOrInsertComputed`。当前 Chromium 与 macOS WKWebView
 * 尚未实现，缺少时 `page.render` 会直接抛 TypeError，导致整页显示“渲染失败”。
 * 在任何渲染前补齐实现，保证原生客户端与浏览器都能正常显示原文。
 */
function installGetOrInsertComputed(proto: { getOrInsertComputed?: unknown }) {
  if (typeof proto.getOrInsertComputed === "function") return;
  Object.defineProperty(proto, "getOrInsertComputed", {
    configurable: true,
    writable: true,
    enumerable: false,
    value: function getOrInsertComputed(this: Map<unknown, unknown>, key: unknown, callback: (key: unknown) => unknown) {
      if (this.has(key)) return this.get(key);
      const value = callback(key);
      this.set(key, value);
      return value;
    },
  });
}
installGetOrInsertComputed(Map.prototype as { getOrInsertComputed?: unknown });
installGetOrInsertComputed(WeakMap.prototype as { getOrInsertComputed?: unknown });

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

/**
 * PDF.js v5 把 JBIG2（双色扫描件）、JPEG2000 和 QCMS（ICC 色彩）解码器改成 WASM，
 * 必须显式声明资源目录，否则扫描页只会渲染成白页：解码失败只走 `console.warn`，
 * 不会冒泡成 `getDocument` 的错误，因此界面既没有报错也没有内容。
 *
 * 资源由 Vite 从锁定的 pdfjs-dist 依赖复制进产物，开发与客户端使用同一版本。
 * PDF.js 内部用字符串拼接（`${wasmUrl}jbig2.wasm`），所以目录必须以 `/` 结尾，
 * 否则 `getFactoryUrlProp()` 会抛 `Invalid factory url`。
 */
const assetBase = new URL(import.meta.env.BASE_URL || "/", window.location.href).href;

export const pdfAssetOptions = {
  wasmUrl: `${assetBase}pdfjs/wasm/`,
  cMapUrl: `${assetBase}pdfjs/cmaps/`,
  cMapPacked: true,
  standardFontDataUrl: `${assetBase}pdfjs/standard_fonts/`,
  iccUrl: `${assetBase}pdfjs/iccs/`,
};
