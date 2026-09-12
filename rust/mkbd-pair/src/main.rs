//! mkbd-pair — Rust port of this repo's Microsoft Modern Keyboard (Fingerprint
//! ID, model 1780) pairing tools: lib/mkbd_common.py, test/tk-pair.py,
//! test/optionA-pair.py, test/phase4.py, pairmodernkeyboard.sh, and the
//! optionA/autopair udev/zenity wrapper, folded into one binary. See
//! rust/mkbd-pair/README.md for scope, status, and what is NOT wired in yet.

mod att;
mod autopair;
mod bdaddr;
mod bond;
mod hid;
mod log;
mod mgmt;
mod pair;
mod sock;

use att::{AdoptOpts, CCCD_HANDLES, H_FEATURE, H_LED, H_MSACC, H_NOTIFY};
use autopair::AutoOpts;
use clap::{Parser, Subcommand};
use pair::{Engine, PairOpts};

/// Format like Python's `%g` for the small set of values this tool prints
/// (hold/timeout seconds) — integral values print without a decimal point.
pub fn fmt_g(v: f64) -> String {
    if v.fract() == 0.0 {
        format!("{}", v as i64)
    } else {
        format!("{v}")
    }
}

#[derive(Parser)]
#[command(name = "mkbd-pair", about = "Pair the Microsoft Modern Keyboard (1780) natively on Linux")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Pair the keyboard: USB F1/F2/F3 -> inject the TK -> MGMT Pair Device -> bond -> address adoption.
    /// Replaces `sudo ./pairmodernkeyboard.sh` / `test/tk-pair.py` / `test/optionA-pair.py`.
    Pair(PairArgs),
    /// Standalone bonded GATT provisioning: subscribe CCCDs and hold the link until the
    /// keyboard adopts its new address. Replaces `test/phase4.py`.
    Adopt(AdoptArgs),
    /// udev-triggered detect -> prompt -> pair -> confirm -> reconnect-watch flow.
    /// Replaces optionA/autopair/mkbd-optionA-autopair.
    Auto(AutoArgs),
}

#[derive(clap::Args)]
struct PairArgs {
    #[arg(long, default_value = "hci0")]
    hci: String,
    #[arg(long, help = "local adapter bdaddr (default: hci0's)")]
    adapter: Option<String>,
    /// Use the Option A path (MGMT_OP_ADD_REMOTE_OOB_DATA) instead of the debugfs TK-injection knob.
    #[arg(long = "option-a")]
    option_a: bool,
    #[arg(long = "tk-order", value_parser = ["as-is", "reversed"], default_value = "as-is")]
    tk_order: String,
    #[arg(long = "addr-type", value_parser = clap::value_parser!(u8).range(0..=1), default_value_t = 1)]
    addr_type: u8,
    #[arg(long, default_value_t = 30)]
    timeout: u64,
    /// Capture btmon + dump the filtered SMP trace and dmesg.
    #[arg(long)]
    diag: bool,
    /// Do NOT mask/stop bluetooth.service during the pair.
    #[arg(long = "no-mask")]
    no_mask: bool,
    /// Stop after the pair; skip GATT provisioning / address adoption.
    #[arg(long = "no-adopt")]
    no_adopt: bool,
    #[arg(long = "adopt-rounds", default_value_t = 1)]
    adopt_rounds: u32,
}

#[derive(clap::Args)]
struct AdoptArgs {
    /// keyboard LE static-random address (the F3 addr)
    kbd: String,
    #[arg(long)]
    adapter: Option<String>,
    #[arg(long, default_value = "hci0")]
    hci: String,
    #[arg(long, default_value_t = 1)]
    rounds: u32,
    #[arg(long, default_value_t = 8.0)]
    hold: f64,
    #[arg(long)]
    discover: bool,
    #[arg(long)]
    writes: bool,
    #[arg(long = "read-msacc")]
    read_msacc: bool,
    #[arg(long = "f3-check")]
    f3_check: bool,
    #[arg(long, value_delimiter = ',')]
    cccd: Vec<String>,
    #[arg(long, default_value = "0x0038")]
    led: String,
    #[arg(long, default_value = "0x0041")]
    feature: String,
    #[arg(long, default_value = "0x0024")]
    notify: String,
    #[arg(long = "led-writes", default_value_t = 3)]
    led_writes: u32,
    #[arg(long, value_delimiter = ',')]
    msacc: Vec<String>,
}

#[derive(clap::Args)]
struct AutoArgs {
    #[arg(long, default_value = "hci0")]
    hci: String,
    #[arg(long = "reconnect-timeout", default_value_t = 90)]
    reconnect_timeout: u64,
}

fn parse_int(s: &str) -> Result<u16, String> {
    let s = s.trim();
    if let Some(hex) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        u16::from_str_radix(hex, 16).map_err(|e| e.to_string())
    } else {
        s.parse().map_err(|e: std::num::ParseIntError| e.to_string())
    }
}

fn main() {
    let cli = Cli::parse();
    let code = match cli.cmd {
        Cmd::Pair(a) => run_pair_cmd(a),
        Cmd::Adopt(a) => run_adopt_cmd(a),
        Cmd::Auto(a) => run_auto_cmd(a),
    };
    std::process::exit(code);
}

fn run_pair_cmd(a: PairArgs) -> i32 {
    let opts = PairOpts {
        engine: if a.option_a { Engine::OptionA } else { Engine::DebugfsTk },
        hci: a.hci,
        adapter: a.adapter,
        tk_reversed: a.tk_order == "reversed",
        addr_type: a.addr_type,
        timeout: a.timeout,
        diag: a.diag,
        no_mask: a.no_mask,
        no_adopt: a.no_adopt,
        adopt_rounds: a.adopt_rounds,
    };
    match pair::run_pair(&opts) {
        Ok(o) => {
            print_summary(&o, opts.no_adopt);
            0
        }
        Err(e) => {
            eprintln!("error: {e}");
            1
        }
    }
}

/// The pairmodernkeyboard.sh-style 3-line summary, built from the returned
/// `PairOutcome` instead of grepping a captured log — this now runs
/// in-process, so the structured result is already at hand.
fn print_summary(o: &pair::PairOutcome, no_adopt: bool) {
    let auth = if o.authenticated { "authenticated" } else { "UNAUTHENTICATED" };
    let line2 = if no_adopt {
        format!("paired ({auth}) · address adoption skipped")
    } else if let Some(a) = &o.adopt {
        let cccd = format!("{}/{} CCCDs", a.cccd_ok, a.cccd_total);
        match a.adopted {
            Some(true) => format!("paired ({auth}) · {cccd} · address ADOPTED"),
            Some(false) => format!("paired ({auth}) · {cccd} · address NOT adopted (retry, or --adopt-rounds 2)"),
            None => format!("paired ({auth}) · {cccd}"),
        }
    } else {
        format!("paired ({auth})")
    };
    println!();
    println!("Modern Keyboard  ·  {}", o.addr);
    println!("{line2}");
    println!("unplug USB & power-cycle the keyboard — it reconnects on its own");
}

fn run_adopt_cmd(a: AdoptArgs) -> i32 {
    let adapter = match a.adapter.or_else(|| bond::adapter_bdaddr(&a.hci).ok()) {
        Some(x) => x.to_uppercase(),
        None => {
            eprintln!("error: could not determine adapter bdaddr");
            return 1;
        }
    };
    let parse_list = |v: &[String], default: &[u16]| -> Vec<u16> {
        if v.is_empty() {
            default.to_vec()
        } else {
            v.iter().filter_map(|s| parse_int(s).ok()).collect()
        }
    };
    let opts = AdoptOpts {
        adapter,
        kbd: a.kbd.to_uppercase(),
        rounds: a.rounds,
        hold: a.hold,
        discover: a.discover,
        writes: a.writes,
        read_msacc: a.read_msacc,
        f3_check: a.f3_check,
        cccd: parse_list(&a.cccd, &CCCD_HANDLES),
        led: parse_int(&a.led).unwrap_or(H_LED),
        feature: parse_int(&a.feature).unwrap_or(H_FEATURE),
        notify: parse_int(&a.notify).unwrap_or(H_NOTIFY),
        led_writes: a.led_writes,
        msacc: parse_list(&a.msacc, &H_MSACC),
    };
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("error: run as root");
        return 1;
    }
    att::run_adopt(&opts);
    0
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
