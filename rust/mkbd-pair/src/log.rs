//! Tiny stderr logger matching the info/warn lines of lib/mkbd_common.py's
//! logging helpers. Python also has `ok`/`die`/`hexdump`; this port only
//! needed `info`/`warn` (errors flow through `Result<_, String>` instead of
//! Python's `die()`-as-SystemExit), so those weren't carried over.

use std::io::IsTerminal;

fn c(code: &str, s: &str) -> String {
    if std::io::stderr().is_terminal() {
        format!("\x1b[{code}m{s}\x1b[0m")
    } else {
        s.to_string()
    }
}

pub fn info(msg: impl AsRef<str>) {
    eprintln!("{} {}", c("36", "::"), msg.as_ref());
}

pub fn warn(msg: impl AsRef<str>) {
    eprintln!("{} {}", c("33", "warn:"), msg.as_ref());
}
