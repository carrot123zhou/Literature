/**
 * 内核 token ↔ 字符偏移确定性映射（跨模式批注：React 划词 ⇄ fold 内核）。
 *
 * 重建规则与上游 reader.js `toksText`/`tokPlain` 逐字符一致（Apache-2.0 vendor 契约）：
 * - token 文本：`tokPlain` —— 有 atom 时 `A[a].kind==='math' ? $alt$ : A[a].text`，
 *   有 `x` 时 `$x$`（行内公式源码），否则 `t`；
 * - 空格：**后置** —— `toks[i].s` 为真时在该 token 之后补一个空格（不是前置）。
 * 重建结果与全文逐字符一致才提供映射；不一致（分词有损/文本差异）返回 null，
 * 调用方回退 block 锚 + quote——绝不伪造 char↔token 对应。
 */

type Tok = { t?: string; x?: string; a?: string; s?: number } | string;
type Atom = { kind?: string; alt?: string; text?: string };

export const tokPlain = (tk: Tok, atoms: Record<string, Atom>): string => {
  if (typeof tk === "string") return tk;
  if (tk.a) {
    const atom = atoms[tk.a];
    if (!atom) return "";
    return atom.kind === "math" ? `$${atom.alt ?? ""}$` : (atom.text || "");
  }
  return tk.x ? `$${tk.x}$` : (tk.t || "");
};

export type TokenMap = {
  starts: number[];
  ends: number[];
  rebuilt: string;
  /** 字符区间 → 内核 anchor.tokens [first, last]（含端）；无交集返回 null。 */
  charToToken: (charStart: number, charEnd: number) => [number, number] | null;
  /** anchor.tokens → 字符区间；越界返回 null。 */
  tokenToChar: (ti: number, tj: number) => [number, number] | null;
};

export function buildTokenMap(toks: Tok[] | undefined, atoms: Record<string, Atom> | undefined, fullText: string): TokenMap | null {
  if (!Array.isArray(toks) || !toks.length) return null;
  const atomMap = atoms ?? {};
  const starts: number[] = [];
  const ends: number[] = [];
  const parts: string[] = [];
  let cursor = 0;
  for (const tk of toks) {
    const text = tokPlain(tk, atomMap);
    const spaceAfter = typeof tk === "object" && tk.s ? 1 : 0;
    const start = cursor;
    starts.push(start);
    ends.push(start + text.length);
    parts.push(text + (spaceAfter ? " " : ""));
    cursor = start + text.length + spaceAfter;
  }
  const rebuilt = parts.join("");
  if (rebuilt !== fullText && !fullText.startsWith(rebuilt)) return null;   // 重建与全文不一致：不提供映射
  const charToToken = (charStart: number, charEnd: number): [number, number] | null => {
    if (charEnd <= charStart) return null;
    let first = -1, last = -1;
    for (let i = 0; i < starts.length; i++) {
      if (ends[i] <= charStart || starts[i] >= charEnd) continue;
      if (first < 0) first = i;
      last = i;
    }
    return first >= 0 ? [first, last] : null;
  };
  const tokenToChar = (ti: number, tj: number): [number, number] | null => {
    if (!Number.isInteger(ti) || !Number.isInteger(tj) || ti < 0 || tj < ti || tj >= starts.length) return null;
    return [starts[ti], ends[tj]];
  };
  return { starts, ends, rebuilt, charToToken, tokenToChar };
}

/** 与内核 toksText 同规则：token a..b 文本，`s` 标志为后置空格（用于写行/派生空间一致性校验）。 */
export function toksTextLike(toks: Tok[], a: number, b: number, atoms: Record<string, Atom>): string {
  let out = "";
  for (let i = a; i <= b && i < toks.length; i++) {
    out += tokPlain(toks[i], atoms);
    if ((typeof toks[i] === "object" && (toks[i] as { s?: number }).s) && i < b) out += " ";
  }
  return out;
}

/** 锚索引空间指纹：token 文本以 \u0001 连接后 djb2 —— 产物重折/内容变更即不一致。 */
export function tokenSpaceDigest(toks: Tok[] | undefined, atoms: Record<string, Atom> | undefined): string | null {
  if (!Array.isArray(toks) || !toks.length) return null;
  const atomMap = atoms ?? {};
  let hash = 5381;
  for (const tk of toks) {
    const text = tokPlain(tk, atomMap);
    for (let i = 0; i < text.length; i++) hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
    hash = (hash + 1) >>> 0;   // 分隔（词界变化也可检出）
  }
  return hash.toString(16).padStart(8, "0");
}
