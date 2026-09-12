//! Option A D-Bus pairing path — port of test/optionA-dbus-pair.py (as of
//! `worktree-kernel-leak-fix` commits af3523e/e3e7357, hardware-verified
//! 2026-09-12): the same F1/F2/F3 + LE legacy OOB TK as `pair::Engine::OptionA`,
//! but through bluetoothd's own `Adapter1.AddRemoteLegacyOOB()` D-Bus method
//! with bluetoothd left running throughout, instead of the raw mgmt socket
//! with bluetoothd stopped.
//!
//! History (see test/optionA-dbus-pair.py's own header for the full account):
//! the first attempt used `Adapter1.StartDiscovery()` + polling for a
//! `Device1` object to appear, and failed 5/5 on hardware — BlueZ's
//! discovery/report pipeline never saw the keyboard's directed advertisement.
//! The fix, verified working end to end on hardware, is
//! `Adapter1.ConnectDevice({"Address": addr, "AddressType": "random"})` — a
//! stock (if `[experimental]`-flagged) BlueZ method that connects directly by
//! address, the same mechanism the raw-mgmt path already relies on.
//!
//! Needs a bluetoothd with the `AddRemoteLegacyOOB` D-Bus method (the
//! `optionA/bluez/0001-*.patch` patch) and running with `--experimental`
//! (`ConnectDevice` is experimental). Uses zbus's blocking API — no tokio, no
//! libdbus — with a background thread + channel wrapped around the two calls
//! that can legitimately take a while (`ConnectDevice`, `Device1::Pair`) so a
//! stuck reply doesn't hang this process forever, mirroring the client-side
//! `timeout=` kwargs the Python version passes to dbus-python.
//!
//! UNVERIFIED BY THIS PORT: the Python original was hardware-tested; this
//! Rust translation has not been. Treat it exactly like the rest of this
//! crate — do not wire it in ahead of `test/optionA-dbus-pair.py`'s own
//! verified behavior.

use crate::hid;
use std::collections::HashMap;
use std::sync::mpsc;
use std::time::Duration;
use zbus::blocking::Connection;
use zbus::interface;
use zbus::proxy;
use zbus::zvariant::{ObjectPath, OwnedObjectPath, OwnedValue, Value};

const AGENT_PATH: &str = "/mkbd/optionA/agent";

#[proxy(
    interface = "org.freedesktop.DBus.ObjectManager",
    default_service = "org.bluez",
    default_path = "/"
)]
trait ObjectManager {
    fn get_managed_objects(
        &self,
    ) -> zbus::Result<HashMap<OwnedObjectPath, HashMap<String, HashMap<String, OwnedValue>>>>;
}

#[proxy(interface = "org.bluez.Adapter1", default_service = "org.bluez", assume_defaults = false)]
trait Adapter1 {
    #[zbus(property)]
    fn powered(&self) -> zbus::fdo::Result<bool>;
    // zbus's default snake_case -> PascalCase name derivation capitalizes only the
    // first letter of "oob", giving "AddRemoteLegacyOob" — override explicitly to
    // match the actual method name the optionA/bluez patch registers.
    #[zbus(name = "AddRemoteLegacyOOB")]
    fn add_remote_legacy_oob(&self, address: &str, address_type: &str, tk: &[u8]) -> zbus::Result<()>;
    fn connect_device(&self, properties: HashMap<&str, Value<'_>>) -> zbus::Result<OwnedObjectPath>;
}

#[proxy(interface = "org.bluez.Device1", default_service = "org.bluez", assume_defaults = false)]
trait Device1 {
    fn pair(&self) -> zbus::Result<()>;
    #[zbus(property)]
    fn paired(&self) -> zbus::fdo::Result<bool>;
    #[zbus(property)]
    fn bonded(&self) -> zbus::fdo::Result<bool>;
    #[zbus(property)]
    fn connected(&self) -> zbus::fdo::Result<bool>;
    #[zbus(property)]
    fn set_trusted(&self, value: bool) -> zbus::fdo::Result<()>;
}

#[proxy(
    interface = "org.bluez.AgentManager1",
    default_service = "org.bluez",
    default_path = "/org/bluez"
)]
trait AgentManager1 {
    fn register_agent(&self, agent: &ObjectPath<'_>, capability: &str) -> zbus::Result<()>;
    fn request_default_agent(&self, agent: &ObjectPath<'_>) -> zbus::Result<()>;
    fn unregister_agent(&self, agent: &ObjectPath<'_>) -> zbus::Result<()>;
}

/// NoInputNoOutput agent — auto-accept whatever BlueZ asks. The OOB TK
/// already armed via AddRemoteLegacyOOB is what actually authenticates the
/// legacy pairing; nothing here should normally even get called. RequestPinCode
/// / RequestPasskey return a generic error rather than the Python original's
/// `org.bluez.Error.Rejected` (zbus's typed interface errors are freedesktop
/// error names) — harmless, since this keyboard's OOB pairing never reaches
/// either.
struct NoIoAgent;

#[interface(name = "org.bluez.Agent1")]
impl NoIoAgent {
    fn release(&self) {}

    fn authorize_service(&self, _device: ObjectPath<'_>, _uuid: &str) -> zbus::fdo::Result<()> {
        Ok(())
    }

    fn request_pin_code(&self, _device: ObjectPath<'_>) -> zbus::fdo::Result<String> {
        Err(zbus::fdo::Error::NotSupported("no pin code available".into()))
    }

    fn request_passkey(&self, _device: ObjectPath<'_>) -> zbus::fdo::Result<u32> {
        Err(zbus::fdo::Error::NotSupported("no passkey available".into()))
    }

    fn display_passkey(&self, _device: ObjectPath<'_>, _passkey: u32, _entered: u16) {}

    fn display_pin_code(&self, _device: ObjectPath<'_>, _pincode: &str) {}

    fn request_confirmation(&self, _device: ObjectPath<'_>, passkey: u32) -> zbus::fdo::Result<()> {
        crate::log::info(format!("  agent: RequestConfirmation({passkey}) -> auto-accept"));
        Ok(())
    }

    fn request_authorization(&self, _device: ObjectPath<'_>) -> zbus::fdo::Result<()> {
        Ok(())
    }

    fn cancel(&self) {}
}

pub struct DbusPairOpts {
    pub hci: String,
    pub adapter: Option<String>,
    pub tk_reversed: bool,
    pub connect_timeout: Duration,
    pub pair_timeout: Duration,
}

impl Default for DbusPairOpts {
    fn default() -> Self {
        Self {
            hci: "hci0".to_string(),
            adapter: None,
            tk_reversed: false,
            connect_timeout: Duration::from_secs(20),
            pair_timeout: Duration::from_secs(30),
        }
    }
}

pub struct DbusPairOutcome {
    pub addr: String,
    pub paired: bool,
    pub bonded: bool,
    pub connected: bool,
}

/// Run `f` (a blocking zbus call) on a helper thread and give up waiting
/// after `timeout` — mirrors the client-side `timeout=` kwargs the Python
/// version passes to dbus-python. Like the Python original, this is a
/// best-effort wait: it does not cancel the in-flight D-Bus call, it just
/// stops waiting for it.
fn with_timeout<T, F>(timeout: Duration, f: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce() -> zbus::Result<T> + Send + 'static,
{
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(f());
    });
    match rx.recv_timeout(timeout) {
        Ok(Ok(v)) => Ok(v),
        Ok(Err(e)) => Err(e.to_string()),
        Err(_) => Err(format!("timed out after {timeout:?}")),
    }
}

fn find_adapter_path(conn: &Connection, want_addr: &str) -> Result<OwnedObjectPath, String> {
    let om = ObjectManagerProxyBlocking::new(conn).map_err(|e| e.to_string())?;
    let objs = om.get_managed_objects().map_err(|e| e.to_string())?;
    for (path, ifaces) in objs {
        let Some(props) = ifaces.get("org.bluez.Adapter1") else {
            continue;
        };
        let Some(addr_val) = props.get("Address") else {
            continue;
        };
        let addr: String = addr_val.clone().try_into().unwrap_or_default();
        if addr.eq_ignore_ascii_case(want_addr) {
            return Ok(path);
        }
    }
    Err(format!("no org.bluez.Adapter1 with address {want_addr} on the bus"))
}

pub fn run_dbus_pair(opts: &DbusPairOpts) -> Result<DbusPairOutcome, String> {
    if unsafe { libc::geteuid() } != 0 {
        return Err("run as root".to_string());
    }

    let removed = crate::bond::remove_matching_bluez_devices();
    if !removed.is_empty() {
        println!(
            ":: removed {} stale keyboard device entr{} from BlueZ: {}",
            removed.len(),
            if removed.len() == 1 { "y" } else { "ies" },
            removed.join(", ")
        );
    }

    let conn = Connection::system().map_err(|e| format!("connecting to system bus: {e}"))?;

    let adapter_addr = match &opts.adapter {
        Some(a) => a.to_uppercase(),
        None => crate::bond::adapter_bdaddr(&opts.hci)?.to_uppercase(),
    };
    println!(":: adapter       : {adapter_addr}");
    let adapter_path = find_adapter_path(&conn, &adapter_addr)?;
    println!(":: adapter path  : {}", adapter_path.as_str());

    let adapter = Adapter1ProxyBlocking::builder(&conn)
        .path(adapter_path.clone())
        .map_err(|e| e.to_string())?
        .build()
        .map_err(|e| e.to_string())?;
    if !adapter.powered().map_err(|e| e.to_string())? {
        return Err("adapter not powered".to_string());
    }

    let dev = hid::open_hidraw(hid::VENDOR_IFACE as i64, hid::VID, hid::PID)
        .map_err(|e| format!("opening vendor hidraw: {e}"))?;
    println!(":: USB F1/F2/F3 ...");
    let res = hid::vendor_pairing_exchange(&dev, &adapter_addr)?;
    drop(dev);

    let addr = res.new_addr.clone();
    println!("   bond already on keyboard : {}", res.bond_exists);
    println!("   keyboard addr (was)      : {}", res.current_addr);
    println!("   keyboard addr (new bond) : {addr}");
    println!(
        "   one-time F3 TK           : {}",
        res.tk.iter().map(|b| format!("{b:02X}")).collect::<String>()
    );
    println!("   device name              : {:?}", res.name);

    let mut tk = res.tk;
    if opts.tk_reversed {
        tk.reverse();
    }

    // register a NoInputNoOutput agent (bluetoothd is live the whole time)
    let agent_path = ObjectPath::try_from(AGENT_PATH).map_err(|e| e.to_string())?;
    conn.object_server()
        .at(&agent_path, NoIoAgent)
        .map_err(|e| format!("exporting Agent1 object: {e}"))?;
    let agent_mgr = AgentManager1ProxyBlocking::new(&conn).map_err(|e| e.to_string())?;
    match agent_mgr.register_agent(&agent_path, "NoInputNoOutput") {
        Ok(()) => {}
        Err(e) if e.to_string().contains("AlreadyExists") => {}
        Err(e) => return Err(format!("RegisterAgent failed: {e}")),
    }
    if let Err(e) = agent_mgr.request_default_agent(&agent_path) {
        crate::log::warn(format!("RequestDefaultAgent: {e}"));
    }
    let unregister = || {
        let _ = agent_mgr.unregister_agent(&agent_path);
    };

    println!(
        ":: Adapter1.AddRemoteLegacyOOB({addr}, random, tk={}) over D-Bus ...",
        tk.iter().map(|b| format!("{b:02x}")).collect::<String>()
    );
    if let Err(e) = adapter.add_remote_legacy_oob(&addr, "random", &tk) {
        unregister();
        return Err(format!(
            "AddRemoteLegacyOOB failed: {e}\n  is the patched bluetoothd installed? \
             test/install-optionA-bluetoothd.sh"
        ));
    }
    println!("   stored — kernel now has the TK for this identity");

    println!(
        ":: Adapter1.ConnectDevice({addr}, random), timeout {}s ...",
        opts.connect_timeout.as_secs_f64()
    );
    let device_path = {
        let adapter_for_thread = adapter.clone();
        let addr_for_thread = addr.clone();
        with_timeout(opts.connect_timeout, move || {
            let mut props: HashMap<&str, Value> = HashMap::new();
            props.insert("Address", Value::from(addr_for_thread.as_str()));
            props.insert("AddressType", Value::from("random"));
            adapter_for_thread.connect_device(props)
        })
    };
    let device_path = match device_path {
        Ok(p) => p,
        Err(e) => {
            unregister();
            return Err(format!(
                "ConnectDevice failed: {e}\n  \
                 NotSupported usually means bluetoothd is not running with --experimental \
                 (ConnectDevice is an experimental BlueZ method).\n  \
                 A timeout usually means the keyboard isn't advertising to this adapter \
                 right now — check it's on USB and the F1/F2/F3 exchange above succeeded, \
                 then retry."
            ));
        }
    };
    println!("   connected, Device1 at {}", device_path.as_str());

    let device = Device1ProxyBlocking::builder(&conn)
        .path(device_path.clone())
        .map_err(|e| e.to_string())?
        .build()
        .map_err(|e| e.to_string())?;

    println!(
        ":: Device1.Pair() -> {addr} (timeout {}s) ...",
        opts.pair_timeout.as_secs_f64()
    );
    let pair_result = {
        let device_for_thread = device.clone();
        with_timeout(opts.pair_timeout, move || device_for_thread.pair())
    };
    unregister();
    if let Err(e) = pair_result {
        return Err(format!("Device1.Pair() did not succeed: {e}"));
    }

    let paired = device.paired().unwrap_or(false);
    let bonded = device.bonded().unwrap_or(false);
    let connected = device.connected().unwrap_or(false);
    println!();
    println!("OK  Paired={paired} Bonded={bonded} Connected={connected}");

    if paired && bonded {
        if let Err(e) = device.set_trusted(true) {
            crate::log::warn(format!("Set Trusted failed: {e}"));
        }
        println!(
            "*** D-BUS PAIRING WORKS — bluetoothd handled the whole thing, never stopped ***"
        );
        return Ok(DbusPairOutcome {
            addr,
            paired,
            bonded,
            connected,
        });
    }
    Err(format!("not paired/bonded (Paired={paired} Bonded={bonded})"))
}
