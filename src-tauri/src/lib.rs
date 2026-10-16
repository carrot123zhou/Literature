use serde::{Deserialize, Serialize};
#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::{
    fs::{self, OpenOptions},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::{Duration, Instant},
};
use tauri::{Manager, State};
use tauri_plugin_dialog::DialogExt;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeInfo {
    api_url: String,
    token: String,
    library: PathBuf,
    runtime: PathBuf,
    logs: PathBuf,
    startup_error: Option<String>,
}
struct RuntimeState {
    info: RuntimeInfo,
    child: Mutex<Option<Child>>,
}

#[derive(Deserialize, Serialize)]
struct LibraryLocation {
    library: PathBuf,
}

#[tauri::command]
fn runtime_info(state: State<RuntimeState>) -> Result<RuntimeInfo, String> {
    if let Some(error) = &state.info.startup_error {
        return Err(error.clone());
    }
    let mut child = state.child.lock().map_err(|e| e.to_string())?;
    if let Some(process) = child.as_mut() {
        if let Some(status) = process.try_wait().map_err(|e| e.to_string())? {
            return Err(format!(
                "本地服务已退出（{}）。请检查 {} 中的 sidecar.log。",
                status,
                state.info.logs.display()
            ));
        }
    }
    Ok(state.info.clone())
}

fn managed_path(info: &RuntimeInfo, path: &Path) -> Result<PathBuf, String> {
    if let Some(error) = &info.startup_error {
        return Err(error.clone());
    }
    let canonical = path.canonicalize().map_err(|e| e.to_string())?;
    for root in [&info.library, &info.runtime, &info.logs] {
        if let Ok(root) = root.canonicalize() {
            if canonical.starts_with(root) {
                return Ok(canonical);
            }
        }
    }
    Err("只能打开 Literature 资源库、运行数据和日志中的文件。".into())
}

#[tauri::command]
fn reveal_path(path: PathBuf, state: State<RuntimeState>) -> Result<(), String> {
    let path = managed_path(&state.info, &path)?;
    #[cfg(target_os = "macos")]
    {
        let mut command = Command::new("/usr/bin/open");
        if path.is_file() {
            command.arg("-R");
        }
        let status = command
            .arg("--")
            .arg(&path)
            .status()
            .map_err(|e| e.to_string())?;
        if status.success() {
            Ok(())
        } else {
            Err("Finder 无法打开此位置。".into())
        }
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let status = Command::new("xdg-open")
            .arg(&path)
            .status()
            .map_err(|e| e.to_string())?;
        if status.success() {
            Ok(())
        } else {
            Err("无法打开此位置。".into())
        }
    }
    #[cfg(windows)]
    {
        // 文件用 /select, 在资源管理器中定位；目录直接打开。
        let mut command = Command::new("explorer");
        if path.is_file() {
            command.arg(format!("/select,{}", path.display()));
        } else {
            command.arg(&path);
        }
        // explorer 对成功也返回非零码，不能按 exit status 判定。
        command
            .status()
            .map_err(|e| e.to_string())?;
        Ok(())
    }
}

#[tauri::command]
async fn save_copy(
    app: tauri::AppHandle,
    path: PathBuf,
    state: State<'_, RuntimeState>,
) -> Result<Option<PathBuf>, String> {
    let source = managed_path(&state.info, &path)?;
    if !source.is_file() {
        return Err("请选择一个文件。文件夹可通过 Finder 复制。".into());
    }
    let name = source
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .to_string();
    let destination = app
        .dialog()
        .file()
        .set_title("另存为")
        .set_file_name(name)
        .blocking_save_file();
    if let Some(destination) = destination {
        let destination = destination.into_path().map_err(|e| e.to_string())?;
        // Saving into the managed library could overwrite source/raw/canonical evidence.
        let target = destination
            .parent()
            .ok_or("保存位置无效")?
            .canonicalize()
            .map_err(|e| e.to_string())?;
        for root in [&state.info.library, &state.info.runtime, &state.info.logs] {
            if target.starts_with(root.canonicalize().map_err(|e| e.to_string())?) {
                return Err("请另存到资源库以外的位置，避免覆盖项目数据。".into());
            }
        }
        if destination.is_symlink() {
            return Err("不能覆盖符号链接。".into());
        }
        fs::copy(source, &destination).map_err(|e| e.to_string())?;
        return Ok(Some(destination));
    }
    Ok(None)
}

#[tauri::command]
async fn choose_library(
    app: tauri::AppHandle,
    state: State<'_, RuntimeState>,
) -> Result<Option<PathBuf>, String> {
    if let Some(folder) = app
        .dialog()
        .file()
        .set_title("选择 Literature 资源库（重新启动后使用）")
        .blocking_pick_folder()
    {
        let library = folder
            .into_path()
            .map_err(|e| e.to_string())?
            .canonicalize()
            .map_err(|e| e.to_string())?;
        if library != state.info.library && library.starts_with(&state.info.library) {
            return Err("不能将现有项目内部目录选作资源库。请选择独立文件夹。".into());
        }
        let probe = library.join(format!(".literature-access-{}", uuid::Uuid::new_v4()));
        fs::write(&probe, b"library access check").map_err(|e| format!("该文件夹不可写：{}", e))?;
        fs::remove_file(probe).map_err(|e| e.to_string())?;
        let contents = serde_json::to_vec_pretty(&LibraryLocation {
            library: library.clone(),
        })
        .map_err(|e| e.to_string())?;
        let temporary = state.info.runtime.join("library-location.tmp");
        fs::write(&temporary, contents).map_err(|e| e.to_string())?;
        fs::rename(temporary, state.info.runtime.join("library-location.json"))
            .map_err(|e| e.to_string())?;
        return Ok(Some(library));
    }
    Ok(None)
}

fn backend_command() -> Result<(PathBuf, Vec<String>, PathBuf), String> {
    if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        if let Ok(value) = std::env::var("LITERATURE_SIDECAR") {
            return Ok((PathBuf::from(value), vec![], root));
        }
        let python = std::env::var_os("LITERATURE_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                if cfg!(windows) {
                    root.join(".venv/Scripts/python.exe")
                } else {
                    root.join(".venv/bin/python")
                }
            });
        return Ok((python, vec!["-m".into(), "literature.api".into()], root));
    }
    let executable = std::env::current_exe().map_err(|e| e.to_string())?;
    let directory = executable.parent().ok_or("应用位置无效")?.to_path_buf();
    let sidecar = directory.join(format!("literature-sidecar{}", std::env::consts::EXE_SUFFIX));
    if !sidecar.is_file() {
        return Err("应用缺少内置后端，请重新构建完整客户端。".into());
    }
    Ok((sidecar, vec![], directory))
}

fn start_backend(app: &tauri::AppHandle) -> Result<RuntimeState, Box<dyn std::error::Error>> {
    let support = app
        .path()
        .app_data_dir()?
        .parent()
        .ok_or("应用数据路径无效")?
        .join("Literature");
    let runtime = std::env::var_os("LITERATURE_RUNTIME")
        .map(PathBuf::from)
        .unwrap_or(support);
    let logs = if cfg!(windows) {
        // Windows：%LOCALAPPDATA%/{identifier}/logs（macOS 保持 ~/Library/Logs/Literature）。
        app.path().app_log_dir().map_err(|e| e.to_string())?
    } else {
        app.path().home_dir()?.join("Library/Logs/Literature")
    };
    fs::create_dir_all(&runtime)?;
    fs::create_dir_all(&logs)?;
    let location = runtime.join("library-location.json");
    let library = if let Some(path) = std::env::var_os("LITERATURE_LIBRARY") {
        PathBuf::from(path)
    } else if location.is_file() {
        serde_json::from_slice::<LibraryLocation>(&fs::read(&location)?)?.library
    } else {
        // Existing installations retain their library; a new Mac gets app-owned data.
        let legacy = app
            .path()
            .home_dir()?
            .join("CdoexProject/Literature/library");
        let library = if legacy.is_dir() {
            legacy
        } else {
            runtime.join("library")
        };
        fs::write(
            &location,
            serde_json::to_vec_pretty(&LibraryLocation {
                library: library.clone(),
            })?,
        )?;
        library
    };
    fs::create_dir_all(&library)?;
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    let mut info = RuntimeInfo {
        api_url: format!("http://127.0.0.1:{}", port),
        token: uuid::Uuid::new_v4().simple().to_string(),
        library: library.canonicalize()?,
        runtime: runtime.canonicalize()?,
        logs: logs.canonicalize()?,
        startup_error: None,
    };
    drop(listener);
    let result = (|| -> Result<Child, String> {
        let (program, args, directory) = backend_command()?;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(info.logs.join("sidecar.log"))
            .map_err(|e| e.to_string())?;
        let mut command = Command::new(program);
        command
            .args(args)
            .current_dir(directory)
            .env("LITERATURE_SESSION_TOKEN", &info.token)
            .env("LITERATURE_PORT", port.to_string())
            .env("LITERATURE_LIBRARY", &info.library)
            .env("LITERATURE_RUNTIME", &info.runtime)
            .env("LITERATURE_PARENT_PID", std::process::id().to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::from(log.try_clone().map_err(|e| e.to_string())?))
            .stderr(Stdio::from(log));
        #[cfg(unix)]
        command.process_group(0);
        command
            .spawn()
            .map_err(|e| format!("无法启动内置服务：{}。日志：{}", e, info.logs.display()))
    })();
    let child = match result {
        Ok(child) => Some(child),
        Err(error) => {
            info.startup_error = Some(error);
            None
        }
    };
    Ok(RuntimeState {
        info,
        child: Mutex::new(child),
    })
}

fn stop_backend(state: &RuntimeState) {
    if let Some(mut child) = state.child.lock().unwrap().take() {
        #[cfg(windows)]
        {
            // PyInstaller onefile 在 Windows 上是「引导器 + 子进程」两级；/T 连带杀整棵进程树。
            let _ = Command::new("taskkill")
                .args(["/PID", &child.id().to_string(), "/T", "/F"])
                .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
                .stderr(Stdio::null())
                .status();
            let _ = child.kill();
            let _ = child.wait();
        }
        #[cfg(not(windows))]
        {
            let group = format!("-{}", child.id());
            let _ = Command::new("/bin/kill")
                .args(["-TERM", "--", &group])
                .stderr(Stdio::null())
                .status();
            let deadline = Instant::now() + Duration::from_secs(2);
            while Instant::now() < deadline {
                if child.try_wait().ok().flatten().is_some() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            let _ = Command::new("/bin/kill")
                .args(["-KILL", "--", &group])
                .stderr(Stdio::null())
                .status();
            let _ = child.wait();
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            runtime_info,
            reveal_path,
            save_copy,
            choose_library
        ])
        .setup(|app| {
            let state = start_backend(app.handle()).unwrap_or_else(|error| RuntimeState {
                info: RuntimeInfo {
                    api_url: String::new(),
                    token: String::new(),
                    library: PathBuf::new(),
                    runtime: PathBuf::new(),
                    logs: PathBuf::new(),
                    startup_error: Some(format!(
                        "无法初始化本地资源：{}。请检查资源库位置与文件夹权限。",
                        error
                    )),
                },
                child: Mutex::new(None),
            });
            app.manage(state);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build Literature")
        .run(|handle, event| {
            if let tauri::RunEvent::Exit = event {
                stop_backend(&handle.state::<RuntimeState>());
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    #[cfg(unix)]
    fn exit_stops_backend_process_group() {
        let mut command = Command::new("/bin/sh");
        command
            .args(["-c", "sleep 30 & wait"])
            .process_group(0)
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let child = command.spawn().unwrap();
        let pid = child.id();
        let info = RuntimeInfo {
            api_url: String::new(),
            token: String::new(),
            library: PathBuf::new(),
            runtime: PathBuf::new(),
            logs: PathBuf::new(),
            startup_error: None,
        };
        let state = RuntimeState {
            info,
            child: Mutex::new(Some(child)),
        };
        stop_backend(&state);
        assert!(state.child.lock().unwrap().is_none());
        let status = Command::new("/bin/kill")
            .args(["-0", "--", &format!("-{}", pid)])
            .stderr(Stdio::null())
            .status()
            .unwrap();
        assert!(!status.success());
    }
    #[test]
    fn managed_paths_reject_outside_and_symlink_escapes() {
        let root =
            std::env::temp_dir().join(format!("literature-path-test-{}", uuid::Uuid::new_v4()));
        let library = root.join("library");
        fs::create_dir_all(&library).unwrap();
        let outside = root.join("outside.txt");
        fs::write(&outside, "evidence").unwrap();
        let info = RuntimeInfo {
            api_url: String::new(),
            token: String::new(),
            library: library.clone(),
            runtime: library.clone(),
            logs: library.clone(),
            startup_error: None,
        };
        assert!(managed_path(&info, &outside).is_err());
        assert!(managed_path(&info, &library).is_ok());
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&outside, library.join("escape")).unwrap();
            assert!(managed_path(&info, &library.join("escape")).is_err());
        }
        fs::remove_dir_all(root).unwrap();
    }
}
