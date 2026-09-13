//! Embeds the current git commit (short hash, +"-dirty" if the tree has
//! uncommitted changes) into the binary as GIT_HASH, so `mkbd-pair --version`
//! can report exactly which commit it was built from -- e.g. to tell whether
//! an installed /usr/local/bin/mkbd-pair actually picked up a given fix.
//! No rerun-if-changed directives on purpose: omitting them makes cargo
//! rerun this script on every build (its default when a build script gives
//! no hints), which is what we want for a value that should always be
//! current -- and `git rev-parse` from rust/mkbd-pair works whether this is
//! a normal checkout or a git worktree (its `.git` is a file, not a dir,
//! pointing at the real gitdir; git itself resolves that transparently).

use std::process::Command;

fn main() {
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
