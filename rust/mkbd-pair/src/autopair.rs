//! Auto-detect -> prompt -> pair -> confirm -> prompt-disconnect ->
//! confirm-reconnect, end to end, for the Microsoft Modern Keyboard over the
//! Option A kernel path. Port of
//! optionA/autopair/mkbd-optionA-autopair (bash + udev + zenity/notify-send),
//! folded into this binary as `mkbd-pair auto` instead of shelling out to
//! pairmodernkeyboard.sh — the pairing engine now runs in-process, so the
//! result is read directly off the `PairOutcome` instead of grepping a log
//! file for the wrapper script's summary lines.
//!
//! Needs: root (mgmt socket + hidraw), the Option A patched bluetooth.ko
//! booted, zenity + notify-send in the logged-in graphical session.

use crate::pair::{self, Engine, PairOpts};
use std::fs::OpenOptions;
use std::os::fd::AsRawFd;
use std::process::{Command, Stdio};
use std::time::Duration;

const LOCK_PATH: &str = "/run/lock/mkbd-pair-autopair.lock";
const USB_MATCH_VID_PID: (&str, &str) = ("045e", "081"); // model byte varies; prefix match like the bash USB_MATCH regex

pub struct AutoOpts {
    pub hci: String,
    pub reconnect_timeout: u64,
}

impl Default for AutoOpts {
    fn default() -> Self {
        Self {
            hci: "hci0".to_string(),
            reconnect_timeout: 90,
        }
    }
}

fn log(msg: impl AsRef<str>) {
    eprintln!("[mkbd-pair auto] {}", msg.as_ref());
}

/// Acquire the run-once flock, mirroring the bash script's `exec 9>"$LOCK";
/// flock -n 9`. Returns None (caller should exit 0, not treat it as failure)
/// if another instance already holds it.
fn acquire_lock() -> Option<std::fs::File> {
    let f = OpenOptions::new().create(true).write(true).truncate(false).open(LOCK_PATH).ok()?;
    let rc = unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc != 0 {
        return None;
    }
    Some(f)
}

fn usb_present() -> bool {
    let out = Command::new("lsusb").output();
    let Ok(out) = out else { return false };
    let text = String::from_utf8_lossy(&out.stdout).to_lowercase();
    text.lines().any(|l| l.contains(USB_MATCH_VID_PID.0) && l.contains(USB_MATCH_VID_PID.1))
}

struct GuiSession {
    user: String,
    runtime_dir: String,
    wayland_display: Option<String>,
    x_display: Option<String>,
    dbus_addr: String,
}

fn find_gui_session() -> Option<GuiSession> {
    let out = Command::new("loginctl").args(["list-sessions", "--no-legend"]).output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let user = text.lines().find_map(|l| {
        let cols: Vec<&str> = l.split_whitespace().collect();
        let u = cols.get(2)?;
        if *u != "root" {
            Some(u.to_string())
        } else {
            None
        }
    })?;

    let uid_out = Command::new("id").args(["-u", &user]).output().ok()?;
    let uid = String::from_utf8_lossy(&uid_out.stdout).trim().to_string();
    let runtime_dir = format!("/run/user/{uid}");

    let env_out = Command::new("sudo")
        .args(["-u", &user, "systemctl", "--user", "show-environment"])
        .env("XDG_RUNTIME_DIR", &runtime_dir)
        .output()
        .ok();
    let env_text = env_out.map(|o| String::from_utf8_lossy(&o.stdout).into_owned()).unwrap_or_default();
    let get = |key: &str| -> Option<String> {
        env_text.lines().find_map(|l| l.strip_prefix(&format!("{key}=")).map(str::to_string))
    };

    let dbus_addr = get("DBUS_SESSION_BUS_ADDRESS").unwrap_or_else(|| format!("unix:path={runtime_dir}/bus"));

    Some(GuiSession {
        user,
        runtime_dir,
        wayland_display: get("WAYLAND_DISPLAY"),
        x_display: get("DISPLAY"),
        dbus_addr,
    })
}

impl GuiSession {
    fn run_as_user(&self, cmd: &str, args: &[&str]) -> std::io::Result<std::process::Output> {
        let mut c = Command::new("sudo");
        c.arg("-u").arg(&self.user);
        c.env("XDG_RUNTIME_DIR", &self.runtime_dir);
        c.env("DBUS_SESSION_BUS_ADDRESS", &self.dbus_addr);
        if let Some(w) = &self.wayland_display {
            c.env("WAYLAND_DISPLAY", w);
        }
        if let Some(d) = &self.x_display {
            c.env("DISPLAY", d);
        }
        c.arg(cmd).args(args);
        c.stdout(Stdio::null()).stderr(Stdio::null());
        c.output()
    }

    fn notify(&self, text: &str, urgency: Option<&str>) {
        let mut args = vec!["Modern Keyboard", text];
        if let Some(u) = urgency {
            args.push("-u");
            args.push(u);
        }
        let _ = self.run_as_user("notify-send", &args);
    }

    fn ask(&self, text: &str) -> bool {
        self.run_as_user(
            "zenity",
            &["--question", "--title=Modern Keyboard", "--width=360", &format!("--text={text}")],
        )
        .map(|o| o.status.success())
        .unwrap_or(false)
    }

    fn info_box(&self, text: &str) {
        let _ = self.run_as_user(
            "zenity",
            &["--info", "--title=Modern Keyboard", "--width=360", &format!("--text={text}")],
        );
    }

    fn error_box(&self, text: &str) {
        let _ = self.run_as_user(
            "zenity",
            &["--error", "--title=Modern Keyboard", "--width=360", &format!("--text={text}")],
        );
    }
}

pub fn run_auto(opts: &AutoOpts) -> Result<(), String> {
    if unsafe { libc::geteuid() } != 0 {
        return Err("run as root".to_string());
    }

    let Some(_lock) = acquire_lock() else {
        log("another mkbd-pair auto is already running — exiting");
        return Ok(());
    };

    let Some(session) = find_gui_session() else {
        log("no non-root graphical session found — cannot prompt; aborting");
        return Ok(());
    };

    if !usb_present() {
        log("045e:081x not on USB — nothing to do");
        return Ok(());
    }

    log(format!("keyboard detected on USB, prompting {}", session.user));
    if !session.ask("A Microsoft Modern Keyboard was plugged in.\n\nPair it over Bluetooth now?") {
        log("user declined");
        return Ok(());
    }

    session.notify("Pairing…", None);
    let pair_opts = PairOpts {
        engine: Engine::OptionA,
        hci: opts.hci.clone(),
        ..Default::default()
    };
    let outcome = match pair::run_pair(&pair_opts) {
        Ok(o) => o,
        Err(e) => {
            log(format!("pairing failed: {e}"));
            session.error_box(&format!("Pairing failed.\n\n{e}"));
            return Err(e);
        }
    };
    log(format!("paired: {}", outcome.addr));

    session.info_box(&format!(
        "Keyboard paired ({}).\n\nUnplug the USB cable now — it will reconnect automatically over Bluetooth.",
        outcome.addr
    ));

    let mut t = 0u64;
    while usb_present() {
        std::thread::sleep(Duration::from_secs(1));
        t += 1;
        if t >= opts.reconnect_timeout {
            log(format!(
                "USB cable never unplugged within {}s — giving up on the reconnect watch",
                opts.reconnect_timeout
            ));
            return Ok(());
        }
    }
    log(format!("USB unplugged after {t}s, watching for the Bluetooth reconnect"));

    let mut t = 0u64;
    while t < opts.reconnect_timeout {
        let out = Command::new("bluetoothctl").args(["info", &outcome.addr]).output();
        if let Ok(out) = out {
            let text = String::from_utf8_lossy(&out.stdout);
            if text.lines().any(|l| l.trim() == "Connected: yes") {
                log(format!("connected over Bluetooth after {t}s"));
                session.notify("Connected over Bluetooth.", None);
                return Ok(());
            }
        }
        std::thread::sleep(Duration::from_secs(1));
        t += 1;
    }
    log(format!("did not see a Bluetooth reconnect within {}s", opts.reconnect_timeout));
    session.notify(
        &format!(
            "Did not reconnect within {}s — try: bluetoothctl connect {}",
            opts.reconnect_timeout, outcome.addr
        ),
        Some("critical"),
    );
    Err("reconnect not observed within timeout".to_string())
}
