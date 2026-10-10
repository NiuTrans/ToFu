// We need a build script to use `link = "tofu-rayon-core"`.  But we're not
// *actually* linking to anything, just making sure that we're the only
// private Tofu Rayon core in use. Upstream Rayon can coexist independently.
fn main() {
    // we don't need to rebuild for anything else
    println!("cargo:rerun-if-changed=build.rs");
}
