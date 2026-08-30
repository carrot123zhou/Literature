use std::{path::{Path, PathBuf}, process::{Child, Command, Stdio}, sync::Mutex};
use tauri::{Manager, State};

struct RuntimeState { token: String, child: Mutex<Option<Child>> }

#[tauri::command]
fn session_token(state: State<RuntimeState>) -> String { state.token.clone() }

fn bundled_sidecar() -> Option<PathBuf> {
    let executable = std::env::current_exe().ok()?;
    let directory = executable.parent()?;
    ["literature-sidecar", "literature-sidecar-aarch64-apple-darwin", "literature-sidecar-x86_64-apple-darwin"]
        .iter().map(|name| directory.join(name)).find(|path| path.is_file())
}

fn python_command() -> (PathBuf, Vec<String>, PathBuf) {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf();
    if let Ok(value) = std::env::var("LITERATURE_SIDECAR") {
        let sidecar = PathBuf::from(value);
        let directory = sidecar.parent().unwrap_or(Path::new(".")).to_path_buf();
        return (sidecar, vec![], directory);
    }
    if let Some(sidecar) = bundled_sidecar() {
        let directory = sidecar.parent().unwrap_or(Path::new(".")).to_path_buf();
        return (sidecar, vec![], directory);
    }
    if let Ok(value) = std::env::var("LITERATURE_PYTHON") { return (PathBuf::from(value), vec!["-m".into(), "literature.api".into()], root); }
    let venv = root.join(".venv/bin/python");
    if venv.is_file() { return (venv, vec!["-m".into(), "literature.api".into()], root); }
    (PathBuf::from("python3"), vec!["-m".into(), "literature.api".into()], root)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let token = uuid::Uuid::new_v4().simple().to_string();
    let state = RuntimeState { token: token.clone(), child: Mutex::new(None) };
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(state)
        .invoke_handler(tauri::generate_handler![session_token])
        .setup(move |app| {
            let (program, args, directory) = python_command();
            let child = Command::new(program).args(args).current_dir(directory)
                .env("LITERATURE_SESSION_TOKEN", &token)
                .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn();
            if let Ok(child) = child { *app.state::<RuntimeState>().child.lock().unwrap() = Some(child); }
            Ok(())
        })
        .build(tauri::generate_context!()).expect("failed to build Literature")
        .run(|handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(mut child) = handle.state::<RuntimeState>().child.lock().unwrap().take() { let _ = child.kill(); }
            }
        });
}
