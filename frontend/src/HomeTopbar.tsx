import type { ReactNode } from "react";

export type TopbarView = "home" | "resources" | "history" | "review" | "read";
export type WorkView = "review" | "read";

type ProjectInfo = {
  title: string;
  subtitle?: string;
  usage?: ReactNode;
};

type Props = {
  active: TopbarView;
  openHome: () => void;
  openResources: () => void;
  /**
   * 工作区标签的统一入口。没有打开项目时也能点击：由调用方决定进入
   * 该模块的空白页并给出提示，而不是渲染成不可用的灰色死按钮。
   */
  openWork: (view: WorkView) => void;
  /** 传入后顶栏标题区显示项目名与用量；未传入时三个标签呈提示态。 */
  project?: ProjectInfo;
  /** 关闭当前任务（清空项目上下文，停留在当前模块）。 */
  closeProject: () => void;
  openSettings: () => void;
};

const WORK_TABS: { view: WorkView; label: string }[] = [
  { view: "review", label: "结构校对" },
  { view: "read", label: "阅读与翻译" },
];

// 主导航是全部页面共用的唯一外壳：首页 → 文件资源 → 结构校对 → 阅读与翻译。
// 打开项目后两个工作区标签激活；未打开项目时点击会得到“先去文件资源打开任务”的引导。
export default function HomeTopbar({ active, openHome, openResources, openWork, project, closeProject, openSettings }: Props) {
  return <header className={`intake-topbar home-topbar${project ? " project-topbar" : ""}`}>
    <div className="intake-brand-group">
      <button className="intake-brand" onClick={openHome}>Literature</button>
      {project && <span className="project-topbar-title"><strong>{project.title}</strong>{project.subtitle && <small>{project.subtitle}</small>}</span>}
      {project && <button className="project-close-button" title="关闭当前任务" aria-label="关闭当前任务" onClick={closeProject}>✕</button>}
    </div>
    <nav aria-label="主导航">
      <button className={active === "home" ? "active" : ""} onClick={openHome}>首页</button>
      <button className={active === "resources" ? "active" : ""} onClick={openResources}>文件资源</button>
      {WORK_TABS.map(tab => <button
        key={tab.view}
        className={`${active === tab.view ? "active" : ""}${project ? "" : " is-hint"}`}
        title={project ? undefined : "请先在「文件资源」打开一篇已完成处理的文章"}
        onClick={() => openWork(tab.view)}
      >{tab.label}</button>)}
    </nav>
    <div className="project-topbar-right">
      {project?.usage}
      <button className="settings-button" onClick={openSettings}>⚙ 设置</button>
    </div>
  </header>;
}
