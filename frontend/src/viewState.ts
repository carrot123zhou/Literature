import { useCallback, useEffect, useState } from "react";

/**
 * 轻量 hash 路由：把「当前视图 + 当前项目」变成可刷新、可后退、可分享的地址。
 *
 * - `#/home`、`#/resources`：首页与文件资源库。
 * - `#/p/<projectId>/review|read|export`：某个项目的三个工作区。
 *
 * 项目上下文独立于视图之外：即使正在浏览首页/文件资源，最近打开的项目仍会
 * 保留在顶栏，用户随时可以点回「结构校对 / 阅读与翻译 / 导出」。
 */
export type View = "home" | "resources" | "review" | "read" | "export";
export type Route = { view: View; projectId: string };

export const WORK_VIEWS: View[] = ["review", "read", "export"];

const LAST_PROJECT_KEY = "literature.lastProject";

export function isWorkView(view: View): boolean {
  return WORK_VIEWS.includes(view);
}

export function parseHash(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "p" && parts[1]) {
    const candidate = parts[2] as View | undefined;
    const view = candidate && isWorkView(candidate) ? candidate : "review";
    return { view, projectId: decodeURIComponent(parts[1]) };
  }
  // 工作区可以不带项目进入：显示该模块的空白页与任务选择列表。
  if (parts[0] && isWorkView(parts[0] as View)) return { view: parts[0] as View, projectId: "" };
  if (parts[0] === "resources") return { view: "resources", projectId: "" };
  return { view: "home", projectId: "" };
}

export function hashFor(view: View, projectId: string): string {
  if (isWorkView(view)) return projectId ? `#/p/${encodeURIComponent(projectId)}/${view}` : `#/${view}`;
  return view === "resources" ? "#/resources" : "#/home";
}

export function readLastProject(): string {
  try { return window.localStorage.getItem(LAST_PROJECT_KEY) || ""; } catch { return ""; }
}

export function writeLastProject(projectId: string): void {
  try {
    if (projectId) window.localStorage.setItem(LAST_PROJECT_KEY, projectId);
    else window.localStorage.removeItem(LAST_PROJECT_KEY);
  } catch { /* localStorage 不可用时仅影响记忆功能。 */ }
}

/**
 * 订阅 hash 变化。`navigate` 写地址栏并触发 hashchange，因此浏览器后退/前进
 * 与页面内跳转共用同一条状态更新路径，不会出现 state 与 URL 双写回环。
 */
export function useHashRoute(): [Route, (view: View, projectId: string) => void] {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const navigate = useCallback((view: View, projectId: string) => {
    const next = hashFor(view, projectId);
    if (window.location.hash === next) setRoute(parseHash(next));
    else window.location.hash = next;
    if (isWorkView(view)) window.scrollTo({ top: 0 });
  }, []);

  return [route, navigate];
}
