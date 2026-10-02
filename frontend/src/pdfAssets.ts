import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

/**
 * PDF.js v5 把 JBIG2（双色扫描件）、JPEG2000 和 QCMS（ICC 色彩）解码器改成 WASM，
 * 必须显式声明资源目录，否则扫描页只会渲染成白页：解码失败只走 `console.warn`，
 * 不会冒泡成 `getDocument` 的错误，因此界面既没有报错也没有内容。
 *
 * 资源由 `frontend/public/pdfjs/` 提供，构建时被原样拷贝到站点根目录。
 * PDF.js 内部用字符串拼接（`${wasmUrl}jbig2.wasm`），所以目录必须以 `/` 结尾，
 * 否则 `getFactoryUrlProp()` 会抛 `Invalid factory url`。
 */
const assetBase = import.meta.env.BASE_URL || "/";

export const pdfAssetOptions = {
  wasmUrl: `${assetBase}pdfjs/wasm/`,
  iccUrl: `${assetBase}pdfjs/iccs/`,
};
