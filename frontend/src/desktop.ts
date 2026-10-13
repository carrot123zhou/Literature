import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";

export const isDesktop = "__TAURI_INTERNALS__" in window;
export type RuntimeInfo = { apiUrl: string; token: string; library: string; runtime: string; logs: string };
export const runtimeInfo = () => invoke<RuntimeInfo>("runtime_info");
export const revealPath = (path: string) => invoke<void>("reveal_path", { path });
export const saveCopy = (path: string) => invoke<string | null>("save_copy", { path });
export const chooseLibrary = () => invoke<string | null>("choose_library");
export async function pickDocuments(extensions: string[]) {
  const selected = await open({ multiple: true, directory: false, title: "选择文献", filters: [{ name: "文献文件", extensions }] });
  return selected ? Array.isArray(selected) ? selected : [selected] : [];
}
