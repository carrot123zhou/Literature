import type { TocNode } from "./types";

export default function TocTree({ nodes, checked, onToggle, onSelect }: { nodes: TocNode[]; checked: Set<string>; onToggle: (id: string) => void; onSelect: (id: string) => void }) {
  return <ul className="toc-tree">{nodes.map(node => <li key={node.node_id}>
    <div className="toc-row" style={{ paddingLeft: `${(node.level-1)*12}px` }}>
      <input type="checkbox" checked={checked.has(node.block_id)} onChange={() => onToggle(node.block_id)}/>
      <button onClick={() => onSelect(node.block_id)}>{node.title}</button><span>{node.page_index+1}</span>
    </div>
    {node.children.length > 0 && <TocTree nodes={node.children} checked={checked} onToggle={onToggle} onSelect={onSelect}/>}
  </li>)}</ul>;
}
