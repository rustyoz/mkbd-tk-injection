//! Embeds the current git commit (short hash, +"-dirty" if the tree has
//! uncommitted changes) into the binary as GIT_HASH, so `mkbd-pair --version`
//! can report exactly which commit it was built from -- e.g. to tell whether
//! an installed /usr/local/bin/mkbd-pair actually picked up a given fix.
//!
//! Correctly tracking "did HEAD change" would mean resolving HEAD's symbolic
//! ref and watching whichever ref file it points at (different again for a
//! git worktree, whose .git is a file pointing at a separate gitdir) --
//! more machinery than this is worth. Instead this watches a path that can
//! never exist: cargo can't cache an "unchanged" fingerprint for a file it
//! can't stat, so it reruns this script on every build, which is exactly
//! what a value that must always be current wants.

use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=NEVER_EXISTS_FORCES_ALWAYS_RERUN");

    let hash = Command::new("git")
        .args(["rev-parse", "--short=12", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|| "unknown".to_string());

    let dirty = Command::new("git")
        .args(["status", "--porcelain"])
        .output()
        .ok()
        .map(|o| !o.stdout.is_empty())
        .unwrap_or(false);

    println!("cargo:rustc-env=GIT_HASH={hash}{}", if dirty { "-dirty" } else { "" });
}
