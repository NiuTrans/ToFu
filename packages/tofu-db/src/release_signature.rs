//! Signed release packaging primitives: Ed25519 key generation, detached
//! signatures, and one bounded verification gate for a packaged release
//! binary against its signed checksum manifest.
//!
//! The release flow is:
//!
//! 1. `scripts/release_package.py` builds the deterministic tarball and the
//!    SHA-256 checksum manifest, then signs the manifest with the operator's
//!    secret key, producing `<base>.SHA256SUMS.sig` (the public key id and
//!    the 64-byte Ed25519 signature, hex encoded).
//! 2. The distribution root publishes the tofu release signing public key in
//!    `packages/tofu-db/keys/release-signing-public.json`; operators may
//!    instead pass `--pubkey <path>` to pin a key out of tree.
//! 3. `tofu-db certify-release --manifest <SHA256SUMS> --signature
//!    <SHA256SUMS.sig> --binary <packaged tofu-db> [--pubkey <path>]` is the
//!    explicit gate: it verifies the Ed25519 signature over the exact
//!    manifest bytes, then matches the packaged binary's SHA-256 against the
//!    manifest's `tofu-db (stripped, packaged)` line. It is an operator
//!    command, never a startup or wire path: the daemon never auto-verifies
//!    anything at boot.
//!
//! Trust model: the public key file is the trust root (distributed with the
//! release tree, pinnable by the operator). The signature proves the manifest
//! — and through the content-addressed SHA-256 lines, the binary — was
//! produced by the holder of the matching secret key. Tampering with the
//! manifest, swapping the binary, or resigning with a different key all fail
//! closed. Key custody is an operator concern: the secret key never enters
//! the repository, and `release_package.py` zeroizes it after signing.
//!
//! Every input is bounded before allocation: key files ≤ 4 KiB, signature
//! files ≤ 1 KiB, manifest files ≤ 64 KiB, and the binary is hashed
//! incrementally (1 MiB chunks), so no file size can force a large resident
//! allocation.

use std::fs;
use std::io::{self, Read};
use std::path::Path;

use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use rand_core::OsRng;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use zeroize::Zeroize;

const MAX_KEY_FILE_BYTES: u64 = 4 << 10;
const MAX_SIGNATURE_FILE_BYTES: u64 = 1 << 10;
const MAX_MANIFEST_BYTES: u64 = 64 << 10;
const HASH_CHUNK_BYTES: usize = 1 << 20;

/// Domain separator for key-file identity digests.
const KEY_IDENTITY_DOMAIN: &[u8] = b"tofu-db.release-key-identity.v1";

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.to_string())
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.to_string())
}

fn read_bounded(path: &Path, maximum: u64, label: &str) -> io::Result<Vec<u8>> {
    let metadata = fs::metadata(path)?;
    if !metadata.is_file() {
        return Err(invalid_input(&format!(
            "{label} is not a regular file: {}",
            path.display()
        )));
    }
    if metadata.len() > maximum {
        return Err(invalid_input(&format!(
            "{label} exceeds the {maximum}-byte bound: {}",
            path.display()
        )));
    }
    // The metadata check alone is racy: a file swapped or extended after the
    // stat would make `fs::read` allocate its full length. Read through one
    // bounded `take` so the allocation can never exceed the cap regardless.
    let file = fs::File::open(path)?;
    let mut contents = Vec::new();
    file.take(maximum.saturating_add(1))
        .read_to_end(&mut contents)?;
    if contents.len() as u64 > maximum {
        return Err(invalid_input(&format!(
            "{label} exceeds the {maximum}-byte bound: {}",
            path.display()
        )));
    }
    Ok(contents)
}

fn hex_decode(value: &str, expected_bytes: usize, label: &str) -> io::Result<Vec<u8>> {
    let value = value.trim();
    if value.len() != expected_bytes * 2 || !value.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(invalid_data(&format!(
            "{label} must be {} lowercase/uppercase hex characters",
            expected_bytes * 2
        )));
    }
    let mut out = Vec::with_capacity(expected_bytes);
    let bytes = value.as_bytes();
    for pair in bytes.chunks_exact(2) {
        let high = (pair[0] as char).to_digit(16).expect("validated hex");
        let low = (pair[1] as char).to_digit(16).expect("validated hex");
        out.push(((high << 4) | low) as u8);
    }
    Ok(out)
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// Key identity fingerprint: SHA-256 over the domain separator and the raw
/// key bytes. Public keys are identified by their own bytes; secret keys by
/// their *verifying* key, so both sides of a pair report the same id.
pub fn key_identity(public_bytes: &[u8; 32]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(KEY_IDENTITY_DOMAIN);
    hasher.update(public_bytes);
    hasher.finalize().into()
}

/// An operator release-signing key pair. The secret half zeroizes on drop.
pub struct ReleaseKeyPair {
    signing: SigningKey,
}

impl ReleaseKeyPair {
    /// Generates a fresh key pair from the OS RNG. Key generation is an
    /// explicit operator command, never implicit at packaging time.
    pub fn generate() -> Self {
        Self {
            signing: SigningKey::generate(&mut OsRng),
        }
    }

    /// Loads a key pair from a JSON file of the form
    /// `{"format": "tofu-db.release-signing-secret.v1", "public": "<64 hex>",
    /// "secret": "<64 hex>"}` where both hex strings are 32 raw bytes. The
    /// embedded public half must match the secret's verifying key, so a
    /// mismatched or transplanted pair fails closed.
    pub fn load(path: &Path) -> io::Result<Self> {
        let raw = read_bounded(path, MAX_KEY_FILE_BYTES, "secret key file")?;
        let value: serde_json::Value = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("secret key file is not valid JSON"))?;
        if value.get("format").and_then(|v| v.as_str()) != Some("tofu-db.release-signing-secret.v1")
        {
            return Err(invalid_data("secret key file format marker mismatch"));
        }
        let mut secret_bytes = hex_decode(
            value.get("secret").and_then(|v| v.as_str()).unwrap_or(""),
            32,
            "secret key",
        )?;
        let public_bytes = hex_decode(
            value.get("public").and_then(|v| v.as_str()).unwrap_or(""),
            32,
            "public key",
        )?;
        let mut secret: [u8; 32] = secret_bytes
            .as_slice()
            .try_into()
            .expect("hex_decode bounds the length");
        let pair = Self {
            signing: SigningKey::from_bytes(&secret),
        };
        secret.zeroize();
        secret_bytes.zeroize();
        let verifying = pair.signing.verifying_key().to_bytes();
        if !bool::from(verifying.ct_eq(&to_array(&public_bytes)?)) {
            return Err(invalid_data(
                "embedded public key does not match the secret key's verifying key",
            ));
        }
        Ok(pair)
    }

    /// Writes the key pair to `path` with owner-only permissions. Refuses to
    /// overwrite an existing file: key rotation is an explicit operator act.
    pub fn save(&self, path: &Path) -> io::Result<()> {
        if path.exists() {
            return Err(invalid_input(&format!(
                "refusing to overwrite existing key file: {}",
                path.display()
            )));
        }
        let document = serde_json::json!({
            "format": "tofu-db.release-signing-secret.v1",
            "public": hex_encode(&self.signing.verifying_key().to_bytes()),
            "secret": hex_encode(&self.signing.to_bytes()),
        });
        write_private_file(path, serde_json::to_string_pretty(&document)?.as_bytes())
    }

    /// Writes the public half to `path` (world-readable content; the file
    /// carries no secret material).
    pub fn save_public(&self, path: &Path) -> io::Result<()> {
        let document = serde_json::json!({
            "format": "tofu-db.release-signing-public.v1",
            "public": hex_encode(&self.signing.verifying_key().to_bytes()),
            "key_id": hex_encode(&key_identity(&self.signing.verifying_key().to_bytes())),
        });
        fs::write(path, serde_json::to_string_pretty(&document)?.as_bytes())
    }

    pub fn verifying_key_bytes(&self) -> [u8; 32] {
        self.signing.verifying_key().to_bytes()
    }

    /// Signs `message`, returning the 64-byte detached signature.
    pub fn sign(&self, message: &[u8]) -> [u8; 64] {
        self.signing.sign(message).to_bytes()
    }
}

// ed25519-dalek's SigningKey zeroizes its seed on Drop (feature
// `zeroize`); ReleaseKeyPair relies on that drop glue, so no explicit Drop
// impl is needed here — the wrapper never copies the seed out.

fn to_array(bytes: &[u8]) -> io::Result<[u8; 32]> {
    bytes
        .try_into()
        .map_err(|_| invalid_data("expected exactly 32 bytes"))
}

/// Writes a secret file with owner-only permissions. Unix sets `0o600` at
/// creation; on Windows the file inherits the directory ACL, which for the
/// per-user profile directory is already owner-only, so no extra mode call
/// applies (there is no POSIX mode bit surface).
fn write_private_file(path: &Path, contents: &[u8]) -> io::Result<()> {
    use std::io::Write as _;
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)?.write_all(contents)
}

/// Loads a public key JSON file
/// (`{"format": "tofu-db.release-signing-public.v1", "public": "<64 hex>"}`).
pub fn load_public_key(path: &Path) -> io::Result<VerifyingKey> {
    let raw = read_bounded(path, MAX_KEY_FILE_BYTES, "public key file")?;
    let value: serde_json::Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("public key file is not valid JSON"))?;
    if value.get("format").and_then(|v| v.as_str()) != Some("tofu-db.release-signing-public.v1") {
        return Err(invalid_data("public key file format marker mismatch"));
    }
    let public_bytes = hex_decode(
        value.get("public").and_then(|v| v.as_str()).unwrap_or(""),
        32,
        "public key",
    )?;
    VerifyingKey::from_bytes(&to_array(&public_bytes)?)
        .map_err(|_| invalid_data("public key is not a valid Ed25519 point"))
}

/// A parsed detached-signature file
/// (`{"format": "tofu-db.release-signature.v1", "key_id": "<64 hex>",
/// "signature": "<128 hex>"}`).
pub struct DetachedSignature {
    pub key_id: [u8; 32],
    pub signature: [u8; 64],
}

pub fn load_signature(path: &Path) -> io::Result<DetachedSignature> {
    let raw = read_bounded(path, MAX_SIGNATURE_FILE_BYTES, "signature file")?;
    let value: serde_json::Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("signature file is not valid JSON"))?;
    if value.get("format").and_then(|v| v.as_str()) != Some("tofu-db.release-signature.v1") {
        return Err(invalid_data("signature file format marker mismatch"));
    }
    let key_id = hex_decode(
        value.get("key_id").and_then(|v| v.as_str()).unwrap_or(""),
        32,
        "key id",
    )?;
    let signature = hex_decode(
        value
            .get("signature")
            .and_then(|v| v.as_str())
            .unwrap_or(""),
        64,
        "signature",
    )?;
    Ok(DetachedSignature {
        key_id: to_array(&key_id)?,
        signature: signature
            .as_slice()
            .try_into()
            .expect("hex_decode bounds the length"),
    })
}

/// Writes a detached signature JSON document.
pub fn write_signature(
    path: &Path,
    public_bytes: &[u8; 32],
    signature: &[u8; 64],
) -> io::Result<()> {
    let document = serde_json::json!({
        "format": "tofu-db.release-signature.v1",
        "key_id": hex_encode(&key_identity(public_bytes)),
        "signature": hex_encode(signature),
    });
    fs::write(path, serde_json::to_string_pretty(&document)?.as_bytes())
}

fn sha256_file(path: &Path) -> io::Result<[u8; 32]> {
    let mut file = fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; HASH_CHUNK_BYTES];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    buffer.zeroize();
    Ok(hasher.finalize().into())
}

/// The outcome of a certified release verification, formatted as one
/// credential-free machine line by [`ReleaseVerification::format_line`].
#[derive(Debug)]
pub struct ReleaseVerification {
    pub key_id: [u8; 32],
    pub manifest_sha256: [u8; 32],
    pub binary_sha256: [u8; 32],
}

impl ReleaseVerification {
    pub fn format_line(&self) -> String {
        format!(
            "format=tofu-db.release-certification.v1 key_id={} manifest_sha256={} binary_sha256={} verdict=verified",
            hex_encode(&self.key_id),
            hex_encode(&self.manifest_sha256),
            hex_encode(&self.binary_sha256),
        )
    }
}

/// Verifies a packaged release binary against a signed checksum manifest.
///
/// Steps (each fail-closed):
/// 1. Parse the signature file; its embedded key id must match the supplied
///    public key's identity, so a signature transplanted from a different
///    key is rejected before any cryptographic work.
/// 2. Read the manifest (≤ 64 KiB) and verify the Ed25519 signature over its
///    exact bytes.
/// 3. Extract the expected digest from the manifest's packaged-binary line
///    (`<64 hex>  <base>/tofu-db (stripped, packaged)`), then hash the
///    supplied binary incrementally and compare in constant time.
pub fn verify_release(
    manifest_path: &Path,
    signature_path: &Path,
    binary_path: &Path,
    public_key: &VerifyingKey,
) -> io::Result<ReleaseVerification> {
    let detached = load_signature(signature_path)?;
    let public_bytes = public_key.to_bytes();
    if !bool::from(detached.key_id.ct_eq(&key_identity(&public_bytes))) {
        return Err(invalid_data(
            "signature key id does not match the supplied public key",
        ));
    }
    let manifest = read_bounded(manifest_path, MAX_MANIFEST_BYTES, "checksum manifest")?;
    let signature = Signature::from_bytes(&detached.signature);
    public_key
        .verify(&manifest, &signature)
        .map_err(|_| invalid_data("manifest signature verification failed"))?;

    let text = String::from_utf8(manifest.clone())
        .map_err(|_| invalid_data("checksum manifest is not valid UTF-8"))?;
    let mut expected: Option<[u8; 32]> = None;
    for line in text.lines() {
        // The signed filename includes a space-bearing packaging annotation.
        let Some((digest, name)) = line.trim().split_once(char::is_whitespace) else {
            continue;
        };
        let name = name.trim_start();
        if !name.ends_with("/tofu-db (stripped, packaged)")
            && name != "tofu-db (stripped, packaged)"
        {
            continue;
        }
        if expected.is_some() {
            return Err(invalid_data(
                "checksum manifest contains more than one packaged-binary line",
            ));
        }
        let decoded = hex_decode(digest, 32, "packaged-binary digest")?;
        expected = Some(to_array(&decoded)?);
    }
    let expected = expected.ok_or_else(|| {
        invalid_data("checksum manifest has no packaged-binary (stripped, packaged) line")
    })?;
    let actual = sha256_file(binary_path)?;
    if !bool::from(actual.ct_eq(&expected)) {
        return Err(invalid_data(
            "packaged binary digest does not match the signed manifest",
        ));
    }
    let mut manifest_hasher = Sha256::new();
    manifest_hasher.update(&manifest);
    let manifest_sha256: [u8; 32] = manifest_hasher.finalize().into();
    Ok(ReleaseVerification {
        key_id: detached.key_id,
        manifest_sha256,
        binary_sha256: actual,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir(label: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "tofudb-release-sig-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    struct Fixture {
        root: std::path::PathBuf,
        key: ReleaseKeyPair,
        public_path: std::path::PathBuf,
        manifest_path: std::path::PathBuf,
        signature_path: std::path::PathBuf,
        binary_path: std::path::PathBuf,
    }

    impl Fixture {
        fn new(label: &str) -> Self {
            let root = tempdir(label);
            let key = ReleaseKeyPair::generate();
            let public_path = root.join("release-signing-public.json");
            key.save_public(&public_path).unwrap();
            let manifest_path = root.join("tofu-db-0.2.0.SHA256SUMS");
            let signature_path = root.join("tofu-db-0.2.0.SHA256SUMS.sig");
            let binary_path = root.join("tofu-db");
            let binary_bytes: Vec<u8> = (0u32..300_000).map(|i| (i % 251) as u8).collect();
            fs::write(&binary_path, &binary_bytes).unwrap();
            let digest = sha256_file(&binary_path).unwrap();
            let manifest = format!(
                "# tofu-db 0.2.0 release checksums\n{}  tofu-db-0.2.0.tar.gz\n{}  tofu-db-0.2.0/tofu-db (stripped, packaged)\n",
                "aa".repeat(32),
                hex_encode(&digest),
            );
            fs::write(&manifest_path, &manifest).unwrap();
            let signature = key.sign(manifest.as_bytes());
            write_signature(&signature_path, &key.verifying_key_bytes(), &signature).unwrap();
            Self {
                root,
                key,
                public_path,
                manifest_path,
                signature_path,
                binary_path,
            }
        }

        fn public_key(&self) -> VerifyingKey {
            load_public_key(&self.public_path).unwrap()
        }

        fn verify(&self) -> io::Result<ReleaseVerification> {
            verify_release(
                &self.manifest_path,
                &self.signature_path,
                &self.binary_path,
                &self.public_key(),
            )
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn keypair_round_trip_and_public_match() {
        let root = tempdir("keypair");
        let secret_path = root.join("secret.json");
        let public_path = root.join("public.json");
        let key = ReleaseKeyPair::generate();
        key.save(&secret_path).unwrap();
        key.save_public(&public_path).unwrap();
        let loaded = ReleaseKeyPair::load(&secret_path).unwrap();
        assert_eq!(loaded.verifying_key_bytes(), key.verifying_key_bytes());
        let message = b"manifest bytes";
        assert_eq!(loaded.sign(message), key.sign(message));
        // Refuses to overwrite an existing secret key file.
        assert!(ReleaseKeyPair::generate().save(&secret_path).is_err());
        // The secret file is owner-only. Only Unix exposes POSIX mode bits;
        // Windows owner-only access comes from the profile-directory ACL.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&secret_path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn mismatched_embedded_public_key_fails_closed() {
        let root = tempdir("mismatch");
        let one = ReleaseKeyPair::generate();
        let two = ReleaseKeyPair::generate();
        let document = serde_json::json!({
            "format": "tofu-db.release-signing-secret.v1",
            "public": hex_encode(&two.verifying_key_bytes()),
            "secret": hex_encode(&one.signing.to_bytes()),
        });
        let path = root.join("secret.json");
        fs::write(&path, serde_json::to_string(&document).unwrap()).unwrap();
        assert!(ReleaseKeyPair::load(&path).is_err());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn verify_release_accepts_a_genuine_package() {
        let fixture = Fixture::new("genuine");
        let verification = fixture.verify().unwrap();
        assert!(verification.format_line().contains("verdict=verified"));
    }

    #[test]
    fn tampered_manifest_fails_signature_verification() {
        let fixture = Fixture::new("tampered-manifest");
        let mut manifest = fs::read_to_string(&fixture.manifest_path).unwrap();
        manifest.push_str("# attacker comment\n");
        fs::write(&fixture.manifest_path, manifest).unwrap();
        let error = fixture.verify().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn swapped_binary_fails_digest_match() {
        let fixture = Fixture::new("swapped-binary");
        fs::write(&fixture.binary_path, b"attacker binary").unwrap();
        let error = fixture.verify().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn signature_from_a_different_key_fails_on_key_id() {
        let fixture = Fixture::new("foreign-key");
        let foreign = ReleaseKeyPair::generate();
        let manifest = fs::read(&fixture.manifest_path).unwrap();
        let signature = foreign.sign(&manifest);
        let foreign_path = fixture.root.join("foreign.sig");
        write_signature(&foreign_path, &foreign.verifying_key_bytes(), &signature).unwrap();
        let error = verify_release(
            &fixture.manifest_path,
            &foreign_path,
            &fixture.binary_path,
            &fixture.public_key(),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn signature_over_a_different_manifest_fails() {
        let fixture = Fixture::new("wrong-manifest");
        let signature = fixture.key.sign(b"a different manifest");
        let wrong_path = fixture.root.join("wrong.sig");
        write_signature(&wrong_path, &fixture.key.verifying_key_bytes(), &signature).unwrap();
        let error = verify_release(
            &fixture.manifest_path,
            &wrong_path,
            &fixture.binary_path,
            &fixture.public_key(),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn manifest_without_a_packaged_binary_line_fails_closed() {
        let fixture = Fixture::new("no-binary-line");
        fs::write(&fixture.manifest_path, "# nothing here\n").unwrap();
        // Resign the stripped manifest so the failure is the missing line,
        // not the signature.
        let manifest = fs::read(&fixture.manifest_path).unwrap();
        let signature = fixture.key.sign(&manifest);
        write_signature(
            &fixture.signature_path,
            &fixture.key.verifying_key_bytes(),
            &signature,
        )
        .unwrap();
        let error = fixture.verify().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn oversized_inputs_fail_before_allocation() {
        let fixture = Fixture::new("oversized");
        let huge = fixture.root.join("huge.sig");
        fs::write(&huge, vec![b'x'; (MAX_SIGNATURE_FILE_BYTES + 1) as usize]).unwrap();
        let error = verify_release(
            &fixture.manifest_path,
            &huge,
            &fixture.binary_path,
            &fixture.public_key(),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    }
}
