import { api } from "../api";

export default function InstantPanel({ projectId, locale, value, close, onSaved }: { projectId: string; locale: string; value: { source: string; text: string; blockId: string }; close: () => void; onSaved: () => void }) {
  const save = async () => { if (!value.blockId) return; await api(`/projects/${projectId}/translations/${value.blockId}/selections`, { method: "POST", body: JSON.stringify({ source_text: value.source, translated_text: value.text, locale }) }); await onSaved(); close(); };
  return <div className="instant-card u-card"><div className="panel-title"><strong>即时翻译</strong><button onClick={close}>关闭</button></div><small>{value.source}</small><p>{value.text}</p><button disabled={!value.blockId} className="primary" onClick={save}>保存到当前 block 的译文层</button></div>;
}
