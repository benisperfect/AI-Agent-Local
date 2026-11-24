use std::fs;
use std::path::PathBuf;
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Emitter, LogicalPosition, LogicalSize, Manager, WebviewWindow,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

fn sirius_token_path() -> Option<PathBuf> {
    #[cfg(target_os = "windows")]
    {
        dirs::data_local_dir().map(|base| base.join("Sirius").join("ipc.token"))
    }
    #[cfg(target_os = "macos")]
    {
        dirs::data_dir().map(|base| base.join("Sirius").join("ipc.token"))
    }
    #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
    {
        dirs::data_local_dir().map(|base| base.join("sirius").join("ipc.token"))
    }
}

#[tauri::command]
fn read_ipc_token() -> Result<String, String> {
    let path = sirius_token_path().ok_or_else(|| "Could not resolve data dir".to_string())?;
    fs::read_to_string(&path)
        .map(|s| s.trim().to_string())
        .map_err(|e| format!("Failed reading {:?}: {}", path, e))
}

#[tauri::command]
fn set_click_through(window: WebviewWindow, enabled: bool) -> Result<(), String> {
    window
        .set_ignore_cursor_events(enabled)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn set_drawer_expanded(window: WebviewWindow, expanded: bool) -> Result<(), String> {
    let target_size = if expanded {
        LogicalSize::new(470.0, 585.0)
    } else {
        LogicalSize::new(460.0, 76.0)
    };
    window.set_size(target_size).map_err(|e| e.to_string())
}

fn position_top_right_primary(window: &WebviewWindow) {
    if let Ok(Some(monitor)) = window.primary_monitor() {
        let scale = monitor.scale_factor();
        let phys_w = monitor.size().width as f64 / scale;
        let x = (phys_w - 490.0).max(24.0);
        let _ = window.set_position(LogicalPosition::new(x, 28.0));
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let kill_shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::Space);

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |app, shortcut, event| {
                    if shortcut == &kill_shortcut && event.state() == ShortcutState::Pressed {
                        let _ = app.emit("sirius://emergency-stop", ());
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            read_ipc_token,
            set_click_through,
            set_drawer_expanded,
        ])
        .setup(move |app| {
            let _ = app.global_shortcut().register(kill_shortcut);

            if let Some(main_win) = app.get_webview_window("main") {
                position_top_right_primary(&main_win);
            }

            // System Tray Menu
            let wake_item = MenuItem::with_id(app, "wake", "Wake Sirius (Hey Sirius)", true, None::<&str>)?;
            let toggle_item = MenuItem::with_id(app, "toggle", "Show / Hide HUD", true, None::<&str>)?;
            let stop_item = MenuItem::with_id(app, "stop", "Emergency Stop (Ctrl+Shift+Space)", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "Quit HUD", true, None::<&str>)?;

            let menu = Menu::with_items(app, &[&wake_item, &toggle_item, &stop_item, &quit_item])?;

            let _tray = TrayIconBuilder::new()
                .menu(&menu)
                .tooltip("Sirius Autonomous Voice Agent")
                .on_menu_event(|app_handle, event| match event.id.as_ref() {
                    "wake" => {
                        let _ = app_handle.emit("sirius://wake", ());
                    }
                    "stop" => {
                        let _ = app_handle.emit("sirius://emergency-stop", ());
                    }
                    "toggle" => {
                        if let Some(win) = app_handle.get_webview_window("main") {
                            let visible = win.is_visible().unwrap_or(true);
                            if visible {
                                let _ = win.hide();
                            } else {
                                let _ = win.show();
                            }
                        }
                    }
                    "quit" => {
                        app_handle.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Sirius Tauri v2 HUD");
}
