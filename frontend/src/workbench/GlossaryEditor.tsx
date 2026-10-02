import { useState } from "react";

export default function GlossaryEditor({ entries, setEntries, close, freeze }: { entries: Record<string, string>; setEntries: (value: Record<string, string>) => void; close: () => void; freeze: (value: Record<string, string>) => void }) {
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
  return <div className="modal-backdrop" onClick={close}><section className="settings glossary-editor" role="dialog" aria-modal="true" aria-label="冻结术语表" onClick={event => event.stopPropagation()}><div className="panel-title"><h2>冻结术语表</h2><button onClick={close}>关闭</button></div><p>每行一个术语，原文和译文用 Tab 或 <code>=&gt;</code> 分隔。冻结后随翻译任务保存快照。</p><textarea value={text} onChange={event => setText(event.target.value)} placeholder={'Paradigm\t范式'} /><button className="primary" onClick={save}>确认并冻结</button></section></div>;
}
