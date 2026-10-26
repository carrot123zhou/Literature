import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ModelEntry, ModelService } from "../types";
import "./reader.css";
import type { ReaderModelBinding } from "./types";

type Props = {
  /** 触发按钮的文案标签（如「折叠模型」「对话模型」）。 */
  label: string;
  providers: ModelService[];
  value: ReaderModelBinding;
  onChange: (binding: ReaderModelBinding) => void;
  /** 弹出层对齐方向（贴右边缘的输入区用 right）。 */
  align?: "left" | "right";
  compact?: boolean;
  emptyHint?: string;
  /** 角色未单独绑定时的按钮文案（如「未单独绑定（继承翻译）」）；缺省用通用文案。 */
  unboundLabel?: string;
};

/**
 * 按服务商分组的模型选择器（两层 popover）：服务商标题 → 旗下模型列表。
 * - 所有已配置服务的模型都可达；同名模型靠所属服务商分组区分。
 * - 搜索过滤、键盘 ↑/↓/Enter/Esc（跳过不可用项）、点击外部关闭、320 宽不溢出。
 * - popover 通过 portal 渲染到 body 并用视口坐标定位（上/下翻转 + 高度限制 + 滚动重定位），
 *   避免被 AI 面板 / 设置弹窗的 overflow 裁切；主题跟随所处上下文（阅读器外观或应用令牌）。
 */
export default function ModelPicker({ label, providers, value, onChange, align = "left", compact = false, emptyHint, unboundLabel }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [pos, setPos] = useState<{ top: number; left: number; maxHeight: number; width: number } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  /** 唯一过滤规则：模型名/模型显示名/服务商名都参与匹配（渲染分组与 flat 列表共用，避免不一致）。 */
  const matches = useCallback((service: ModelService, entry: ModelEntry, needle: string): boolean =>
    !needle ||
    entry.model.toLowerCase().includes(needle) ||
    (entry.name || "").toLowerCase().includes(needle) ||
    service.name.toLowerCase().includes(needle), []);

  const flat = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const out: { service: ModelService; entry: ModelEntry; disabled: boolean }[] = [];
    for (const service of providers) {
      for (const entry of service.models) {
        if (matches(service, entry, needle)) out.push({ service, entry, disabled: !service.configured });
      }
    }
    return out;
  }, [providers, query, matches]);

  /** 渲染分组直接从 flat 派生：搜索（含按服务商名搜索）时列表与可用项永远一致。 */
  const groups = useMemo(() => {
    const map = new Map<ModelService, ModelEntry[]>();
    flat.forEach(({ service, entry }) => {
      const list = map.get(service) ?? [];
      list.push(entry);
      map.set(service, list);
    });
    return [...map.entries()];
  }, [flat]);

  const current = providers.find(s => s.service_id === value.service_id);
  const currentEntry = current?.models.find(m => m.model === value.model);
  const unbound = !current;
  const currentLabel = currentEntry
    ? `${currentEntry.model} · ${current?.name ?? ""}`
    : (unboundLabel || (providers.length ? "未选择模型" : "未配置模型"));

  const clampNum = (value: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, value));
  /** 视口定位：优先在触发按钮下方展开，空间不足向上翻转；左右夹在视口内。 */
  const reposition = useCallback(() => {
    const trigger = rootRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    const popW = Math.min(320, vw - 16);
    const left = clampNum(rect.right - popW, 8, Math.max(8, vw - popW - 8));
    const below = vh - rect.bottom - 8;
    const above = rect.top - 8;
    let top: number, maxHeight: number;
    if (below >= 220 || below >= above) {
      top = rect.bottom + 6;
      maxHeight = Math.max(160, Math.min(340, below));
    } else {
      maxHeight = Math.max(160, Math.min(340, above));
      top = rect.top - 6 - maxHeight;
    }
    top = clampNum(top, 8, Math.max(8, vh - 8 - Math.min(maxHeight, 200)));
    maxHeight = Math.min(maxHeight, vh - top - 8);
    setPos(prev => {
      if (prev && prev.top === top && prev.left === left && prev.maxHeight === maxHeight && prev.width === popW) return prev;
      return { top, left, maxHeight, width: popW };
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !popRef.current?.contains(target)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        event.preventDefault();
        setOpen(false);
        (rootRef.current?.querySelector(".rdrr-mpick-btn") as HTMLElement | null)?.focus();
      }
    };
    // rAF 节流：同一帧只重定位一次；portal 内部滚动不触发（避免 scrollIntoView 反馈循环）
    let rafId = 0;
    const repositionSoon = (event?: Event) => {
      if (event && popRef.current?.contains(event.target as Node)) return;
      if (rafId) return;
      rafId = requestAnimationFrame(() => { rafId = 0; reposition(); });
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", repositionSoon, true);
    window.addEventListener("resize", repositionSoon);
    reposition();
    return () => {
      cancelAnimationFrame(rafId);
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("scroll", repositionSoon, true);
      window.removeEventListener("resize", repositionSoon);
    };
  }, [open, reposition]);

  useEffect(() => { if (open) { setActive(0); reposition(); } }, [query, open, reposition]);
  useEffect(() => {
    if (!open) return;
    // scrollIntoView 会触发滚动事件，暂时挂起 scroll→reposition 管线
    const el = listRef.current?.querySelector("[data-active=true]");
    if (!el) return;
    const parent = listRef.current;
    if (!parent) return;
    const prevScroll = parent.scrollTop;
    el.scrollIntoView({ block: "nearest" });
    // scrollIntoView 只应影响 mpick-list 内部滚动；如果它改变了 parent.scrollTop，恢复
    if (parent.scrollTop !== prevScroll) parent.scrollTop = prevScroll;
  }, [active, open]);

  const commit = (index: number) => {
    const item = flat[index];
    // 不可用（未配密钥）服务商的模型不可提交——键盘 Enter 与点击同一守卫
    if (!item || item.disabled) return;
    onChange({ service_id: item.service.service_id, model: item.entry.model });
    setOpen(false);
  };

  /** 键盘移动时跳过不可用项；无可用项则不动。 */
  const moveActive = (delta: 1 | -1) => {
    setActive(current => {
      if (!flat.length) return current;
      let next = current;
      for (let step = 0; step < flat.length; step++) {
        next = next + delta < 0 ? flat.length - 1 : next + delta >= flat.length ? 0 : next + delta;
        if (!flat[next].disabled) return next;
      }
      return current;
    });
  };
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") { event.preventDefault(); moveActive(1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); moveActive(-1); }
    else if (event.key === "Enter") { event.preventDefault(); commit(active); }
  };

  // portal 主题：在阅读器内继承其外观令牌；在设置等应用上下文使用应用级令牌（var 回退）
  const portalInReader = Boolean(rootRef.current?.closest(".rdrr-root"));
  const readerAppearance = rootRef.current?.closest(".rdrr-root")?.getAttribute("data-appearance") ?? "light";

  const popover = open ? createPortal(
    <div
      ref={popRef}
      className={"rdrr-mpick-portal" + (portalInReader ? " rdrr-portal-theme" : "")}
      data-appearance={portalInReader ? readerAppearance : undefined}
      style={pos ? { top: pos.top, left: pos.left, maxHeight: pos.maxHeight, width: pos.width } : { visibility: "hidden", top: -9999, left: -9999, width: 320 }}
      role="listbox"
      aria-label={label}
    >
      <div className="rdrr-mpick-search">
        <input
          ref={searchRef}
          value={query}
          placeholder="搜索服务商或模型…"
          aria-label="搜索模型"
          onChange={event => setQuery(event.target.value)}
          onKeyDown={onKeyDown}
        />
      </div>
      <div className="rdrr-mpick-list" ref={listRef}>
        {!providers.length && <div className="rdrr-mpick-empty">{emptyHint || "尚未配置模型服务商；请在设置 → 模型服务中添加。"}</div>}
        {groups.map(([service, models]) => {
          const missingKey = !service.configured;
          return (
            <div key={service.service_id} className="rdrr-mpick-group">
              <div className="rdrr-mpick-group-head">
                <span className="rdrr-mpick-group-name">{service.name}</span>
                <span className={"rdrr-mpick-group-state" + (missingKey ? " warn" : "")}>
                  {missingKey ? "未配密钥" : `${models.length} 个模型`}
                </span>
              </div>
              {models.map(entry => {
                const index = flat.findIndex(item => item.service === service && item.entry === entry);
                const selected = service.service_id === value.service_id && entry.model === (value.model || service.models[0]?.model);
                const disabled = !service.configured;
                return (
                  <button
                    key={entry.model}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    aria-disabled={disabled}
                    data-active={index === active}
                    className={"rdrr-mpick-item" + (selected ? " sel" : "")}
                    disabled={disabled}
                    title={disabled ? "该服务商未配置密钥，暂不可用" : entry.name || entry.model}
                    onClick={() => commit(index)}
                    onMouseEnter={() => { if (!disabled) setActive(index); }}
                  >
                    <span className="rdrr-mpick-model" title={entry.model}>{entry.model}</span>
                    {entry.name && entry.name !== entry.model && <span className="rdrr-mpick-name" title={entry.name}>{entry.name}</span>}
                    {selected && <span className="rdrr-mpick-check" aria-hidden="true">✓</span>}
                  </button>
                );
              })}
            </div>
          );
        })}
        {providers.length > 0 && !flat.length && <div className="rdrr-mpick-empty">没有匹配的模型</div>}
      </div>
    </div>,
    document.body,
  ) : null;

  return (
    <div className={"rdrr-mpick" + (compact ? " compact" : "")} ref={rootRef}>
      <button
        type="button"
        className="rdrr-mpick-btn"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={label}
        title={currentLabel}
        onClick={() => { setOpen(v => !v); setQuery(""); }}
      >
        <span className="rdrr-mpick-ico" aria-hidden="true">⚙</span>
        <span className="rdrr-mpick-label">{currentLabel}</span>
        <span className="rdrr-mpick-caret" aria-hidden="true">▾</span>
      </button>
      {popover}
    </div>
  );
}
