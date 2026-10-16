import { useState } from "react";
import type { GlossaryVersion } from "../types";

type Props = {
  entries: Record<string, string>;
  setEntries: (value: Record<string, string>) => void;
  versions: GlossaryVersion[];
  close: () => void;
  freeze: (value: Record<string, string>) => void;
  onRollback: (version: number) => void;
};

export default function GlossaryEditor({ entries, setEntries, versions, close, freeze, onRollback }: Props) {
  const [text, setText] = useState(() => Object.entries(entries).map(([source, target]) => `${source}\t${target}`).join("\n"));
  const save = () => {
    const parsed: Record<string, string> = {};
    for (const line of text.split(/\n/)) {
      const [source, ...target] = line.split(/\t|\s*=>\s*/);
      if (source?.trim()) parsed[source.trim()] = target.join(" ").trim();
    }
    setEntries(parsed);
    freeze(parsed);
  };
  return <div className="modal-backdrop glossary-backdrop u-backdrop" onClick={close}><section className="settings glossary-editor u-modal" role="dialog" aria-modal="true" aria-label="冻结术语表" onClick={event => event.stopPropagation()}>
    <div className="panel-title"><h2>术语表</h2><button onClick={close}>关闭</button></div>
    <p>每行一个术语，原文和译文用 Tab 或 <code>=&gt;</code> 分隔。冻结后随翻译任务保存快照。</p>
    <textarea value={text} onChange={event => setText(event.target.value)} placeholder={'Paradigm\t范式'} />
    <button className="primary" onClick={save}>确认并冻结为新版本</button>
    <hr/>
    <h3>版本历史</h3>
    {versions.length === 0 ? <p className="empty-inline">尚无已冻结版本。</p> : <ol className="glossary-versions">
      {[...versions].reverse().map(item => <li key={item.version}>
        <div><b>v{item.version}</b> <small>{item.source} · {new Date(item.created_at).toLocaleString()}</small></div>
        <span>{Object.keys(item.entries).length} 项</span>
        <button onClick={() => onRollback(item.version)}>回滚到此版本</button>
      </li>)}
    </ol>}
  </section></div>;
}
