import { useEffect, useState } from "react";
import { chooseLibrary, isDesktop, revealPath, runtimeInfo, type RuntimeInfo } from "../desktop";

export default function LocalResources({ onMessage }: { onMessage: (message: string) => void }) {
  const [info, setInfo] = useState<RuntimeInfo | null>(null);
  useEffect(() => { if (isDesktop) runtimeInfo().then(setInfo).catch(error => onMessage(String(error))); }, [onMessage]);
  if (!info) return null;
  const reveal = (path: string) => revealPath(path).catch(error => onMessage(String(error)));
  return <section className="local-resources"><h3>本地资源与存储</h3>
    <p className="settings-hint">原件、解析证据与译文保存在资源库。移出的项目保留在资源库 .trash 目录。</p>
    <label>当前资源库<code>{info.library}</code></label>
    <div className="local-resource-actions"><button onClick={() => reveal(info.library)}>打开资源库</button><button onClick={async () => { try { const path = await chooseLibrary(); if (path) onMessage(`已选择 ${path}。重新启动后生效；原资源库仍保留在原位置。`); } catch (error) { onMessage(String(error)); } }}>选择资源库…</button></div>
    <label>运行数据<code>{info.runtime}</code></label><button onClick={() => reveal(info.runtime)}>打开运行数据</button>
    <label>运行日志<code>{info.logs}</code></label><button onClick={() => reveal(info.logs)}>打开日志</button>
  </section>;
}
