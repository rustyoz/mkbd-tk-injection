//! mkbd-pair — Rust port of this repo's Microsoft Modern Keyboard (Fingerprint
//! ID, model 1780) pairing tools: lib/mkbd_common.py, test/optionA-dbus-pair.py,
//! and the optionA/autopair udev/zenity wrapper, folded into one binary. See
//! rust/mkbd-pair/README.md for scope, status, and what is NOT wired in yet.
//!
//! Two things were ported here and then removed after hardware testing:
//!
//! - The raw-mgmt pairing engines (test/tk-pair.py's debugfs path,
//!   test/optionA-pair.py's MGMT_OP_ADD_REMOTE_OOB_DATA path, and
//!   pairmodernkeyboard.sh) required stopping/masking bluetoothd for the
//!   duration of the pair, and a masked-but-not-restored bluetooth.service
//!   from an earlier such run broke Bluetooth entirely until manually
//!   unmasked. The D-Bus path (`dbus-pair`) never touches bluetoothd's
//!   running state and is hardware-verified working, so it's the only
//!   pairing engine left.
//! - `test/phase4.py`'s bonded-GATT "address adoption" step (`adopt`,
//!   src/att.rs + src/sock.rs's raw L2CAP socket plumbing) was ported on the
//!   assumption the D-Bus path would need it too, the way the raw-mgmt path
//!   did. Hardware testing showed `dbus-pair`/`auto` reconnect fine without
//!   it — bluetoothd's own HID-over-GATT profile plugin does the equivalent
//!   subscribing automatically — so it was removed as unused weight.
//!
//! See git history on this branch for either if a future kernel/BlueZ
//! combination needs them again.

mod autopair;
mod bdaddr;
mod bond;
mod dbus_pair;
mod hid;
mod log;

use autopair::AutoOpts;
use clap::{Parser, Subcommand};

/// Set by build.rs from `git rev-parse --short=12 HEAD` (+"-dirty" if the
/// tree had uncommitted changes at build time) -- lets `mkbd-pair --version`
/// answer "is the installed binary actually built from this commit?".
const VERSION: &str = concat!(env!("CARGO_PKG_VERSION"), " (", env!("GIT_HASH"), ")");

#[derive(Parser)]
#[command(
    name = "mkbd-pair",
    about = "Pair the Microsoft Modern Keyboard (1780) natively on Linux",
    version = VERSION
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Pair the keyboard through bluetoothd's own D-Bus surface (AddRemoteLegacyOOB +
    /// ConnectDevice), bluetoothd never stopped. Replaces `test/optionA-dbus-pair.py`.
    DbusPair(DbusPairArgs),
    /// udev-triggered detect -> prompt -> pair -> confirm -> reconnect-watch flow.
    /// Replaces optionA/autopair/mkbd-optionA-autopair.
    Auto(AutoArgs),
}

#[derive(clap::Args)]
struct DbusPairArgs {
    #[arg(long, default_value = "hci0")]
    hci: String,
    #[arg(long)]
    adapter: Option<String>,
    #[arg(long = "tk-order", value_parser = ["as-is", "reversed"], default_value = "as-is")]
    tk_order: String,
    #[arg(long = "connect-timeout", default_value_t = 20.0)]
    connect_timeout: f64,
    #[arg(long = "pair-timeout", default_value_t = 30.0)]
    pair_timeout: f64,
}

#[derive(clap::Args)]
struct AutoArgs {
    #[arg(long, default_value = "hci0")]
    hci: String,
    #[arg(long = "reconnect-timeout", default_value_t = 90)]
    reconnect_timeout: u64,
}

fn main() {
    let cli = Cli::parse();
    let code = match cli.cmd {
        Cmd::DbusPair(a) => run_dbus_pair_cmd(a),
        Cmd::Auto(a) => run_auto_cmd(a),
    };
    std::process::exit(code);
}

fn run_dbus_pair_cmd(a: DbusPairArgs) -> i32 {
    let opts = dbus_pair::DbusPairOpts {
        hci: a.hci,
        adapter: a.adapter,
        tk_reversed: a.tk_order == "reversed",
        connect_timeout: std::time::Duration::from_secs_f64(a.connect_timeout),
        pair_timeout: std::time::Duration::from_secs_f64(a.pair_timeout),
    };
    match dbus_pair::run_dbus_pair(&opts) {
        Ok(o) => {
            println!();
            println!("Modern Keyboard  ·  {}", o.addr);
            println!(
                "paired via D-Bus (Paired={} Bonded={} Connected={}), bluetoothd never stopped",
                o.paired, o.bonded, o.connected
            );
            0
        }
        Err(e) => {
            eprintln!("error: {e}");
            1
        }
    }
}

fn run_auto_cmd(a: AutoArgs) -> i32 {
    let opts = AutoOpts {
        hci: a.hci,
        reconnect_timeout: a.reconnect_timeout,
    };
    match autopair::run_auto(&opts) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("error: {e}");
            1
        }
    }
}
