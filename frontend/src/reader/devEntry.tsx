/**
 * 开发验证入口（dev only）：`vite dev` 打开 /reader-dev.html 使用。
 * 仅用于在浏览器里以真实后端 + 真实项目数据调试 ReaderWorkspace；
 * 不进入生产 index.html（App.tsx 接线由 developer 负责，此文件可在接线后删除）。
 */
import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, initializeApi } from "../api";
import ReaderWorkspace from "./ReaderWorkspace";
import ModelPicker from "./ModelPicker";
import type { DocumentGraph } from "../types";

function DevRoot() {
  const [graph, setGraph] = useState<DocumentGraph | null>(null);
  const [projectId, setProjectId] = useState("");
  const [error, setError] = useState("");

  const [providers, setProviders] = useState<any[]>([]);
  const [roles, setRoles] = useState<any>(null);
  const [translation, setTranslation] = useState<any>(null);
  useEffect(() => {
    initializeApi()
      .then(async () => {
        const projects = await api<{ project_id: string }[]>("/projects");
        if (!projects.length) { setError("隔离库中还没有项目：先运行种子脚本创建。"); return; }
        const wanted = new URLSearchParams(window.location.search).get("project");
        const id = wanted && projects.some(x => x.project_id === wanted) ? wanted : projects[0].project_id;
        setProjectId(id);
        setGraph(await api<DocumentGraph>(`/projects/${id}/document`));
        const ms = await api<{ services: any[]; roles: any }>("/model-services").catch(() => null);
        if (ms) { setProviders(ms.services || []); setRoles(ms.roles || null); }
        const layer = await api<any>(`/projects/${id}/translations/zh-CN`).catch(() => null);
        if (layer) setTranslation(layer);
      })
      .catch(error => setError(String(error)));
  }, []);

  // picker 专用验证模式（dev only）：?pickertest=1 用客户端构造的 mock providers 渲染 ModelPicker
  const params = new URLSearchParams(window.location.search);
  if (params.get("pickertest")) {
    const providers = [
      { service_id: "s1", name: "Alpha AI", preset: "openai", base_url: "http://mock", configured: true,
        models: [{ model: "alpha-large", name: "Alpha Large" }, { model: "alpha-mini" }] },
      { service_id: "s2", name: "Beta AI", preset: "openai", base_url: "http://mock", configured: false,
        models: [{ model: "beta-only" }] },
    ];
    const [binding, setBinding] = useState({ service_id: "", model: "" });
    return (
      <div style={{ padding: 24, fontFamily: "sans-serif" }}>
        <ModelPicker label="对话模型" providers={providers} value={binding} onChange={setBinding} />
        <p data-testid="picked" style={{ marginTop: 12, fontSize: 13 }}>
          {binding.service_id ? `picked:${binding.service_id}/${binding.model}` : "picked:none"}
        </p>
      </div>
    );
  }
  if (error) return <p style={{ padding: 24, fontFamily: "sans-serif" }}>开发验证入口：{error}</p>;
  if (!graph) return <p style={{ padding: 24, fontFamily: "sans-serif" }}>加载中…</p>;
  return <ReaderWorkspace projectId={projectId} document={graph} translation={translation}
    targetLocale={translation?.locale || ""} providers={providers}
    defaultFold={roles?.reader_fold} defaultChat={roles?.reader_chat}
    translationBinding={roles?.translation} />;
}

createRoot(document.getElementById("root")!).render(<DevRoot />);
