/**
 * 对照阅读的坐标契约。
 *
 * MinerU 的 content_list / content_list_v2 在 0-1000 归一化坐标系里给出 bbox，
 * x 按页宽、y 按页高分别缩放。document.json 原样保存这些数值，并在
 * Document.bbox_space 里声明单位，所以任何绘制代码都必须先读该字段。
 */

export const BBOX_SPACE_MINERU_1000 = "mineru-1000";
export const BBOX_SPACE_NONE = "none";

export type BBox = [number, number, number, number];

export function hasBBox(block: { bbox: number[] }): boolean {
  const [x0, y0, x1, y1] = block.bbox;
  return block.bbox.length === 4 && x1 > x0 && y1 > y0;
}

/** 把 bbox 换算成相对页面的百分比矩形，用于绝对定位覆盖层。 */
export function bboxRect(bbox: readonly number[], space: string) {
  const [x0, y0, x1, y1] = bbox;
  const scale = space === BBOX_SPACE_MINERU_1000 ? 1000 : 1;
  return {
    left: (x0 / scale) * 100,
    top: (y0 / scale) * 100,
    width: ((x1 - x0) / scale) * 100,
    height: ((y1 - y0) / scale) * 100,
  };
}

/** 用页面元数据估算渲染前的占位高度，保证未渲染页也占据正确滚动空间。 */
export function estimatedPageHeight(page: { width: number; height: number } | undefined, renderWidth: number, fallbackRatio = 1.414): number {
  const ratio = page && page.width > 0 && page.height > 0 ? page.height / page.width : fallbackRatio;
  return Math.round(renderWidth * ratio);
}

export function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}
