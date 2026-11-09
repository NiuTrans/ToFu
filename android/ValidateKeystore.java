import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.util.Arrays;
import java.util.Locale;
import java.util.Map;

/** JDK-only release guard (Java 11 source launcher or later). Only Gradle's resolved nonsecret file path enters argv. */
class ValidateKeystore {
    private static final class Refused extends RuntimeException {
        Refused(String message) { super(message); }
    }

    private static String required(Map<String, String> env, String name) {
        String value = env.get(name);
        if (value == null || value.isBlank()) {
            throw new Refused("Private release signing requires " + name);
        }
        return value;
    }

    public static void main(String[] args) {
        char[] storePassword = null;
        char[] keyPassword = null;
        try {
            Map<String, String> env = System.getenv();
            if (args.length != 1 || !Path.of(args[0]).isAbsolute()) {
                throw new Refused("Release guard requires the Gradle-resolved absolute keystore path");
            }
            Path path = Path.of(args[0]);
            String alias = required(env, "TOFU_ANDROID_KEY_ALIAS");
            String expected = required(env, "TOFU_ANDROID_SIGNER_SHA256")
                .replace(":", "").toLowerCase(Locale.ROOT);
            if (!expected.matches("[a-f0-9]{64}")) {
                throw new Refused("Release certificate fingerprint must be SHA-256");
            }
            if (alias.equalsIgnoreCase("androiddebugkey")) {
                throw new Refused("The public debug key cannot sign a release");
            }
            if (!Files.isRegularFile(path)) {
                throw new Refused("Release keystore must be a readable file");
            }
            storePassword = required(env, "TOFU_ANDROID_KEYSTORE_PASSWORD").toCharArray();
            keyPassword = required(env, "TOFU_ANDROID_KEY_PASSWORD").toCharArray();
            KeyStore store = KeyStore.getInstance(path.toFile(), storePassword);
            if (!store.isKeyEntry(alias) || store.getKey(alias, keyPassword) == null) {
                throw new Refused("Release alias must identify a private key");
            }
            Certificate certificate = store.getCertificate(alias);
            if (!(certificate instanceof X509Certificate)) {
                throw new Refused("Release signing requires an X.509 certificate");
            }
            X509Certificate x509 = (X509Certificate) certificate;
            x509.checkValidity();
            if (x509.getSubjectX500Principal().getName().toLowerCase(Locale.ROOT).contains("android debug")) {
                throw new Refused("The public debug certificate cannot sign a release");
            }
            StringBuilder fingerprint = new StringBuilder();
            for (byte value : MessageDigest.getInstance("SHA-256").digest(certificate.getEncoded())) {
                fingerprint.append(String.format("%02x", value));
            }
            if (!fingerprint.toString().equals(expected)) {
                throw new Refused("Release certificate fingerprint differs from TOFU_ANDROID_SIGNER_SHA256");
            }
        } catch (Refused error) {
            // Only guard-authored constant messages cross this boundary.
            System.err.println("Private release signing refused: " + error.getMessage());
            System.exit(1);
        } catch (Exception error) {
            // Keystore/provider exceptions may include paths or sensitive input.
            System.err.println("Private release signing refused: could not verify the keystore and certificate");
            System.exit(1);
        } finally {
            if (storePassword != null) Arrays.fill(storePassword, '\0');
            if (keyPassword != null) Arrays.fill(keyPassword, '\0');
        }
    }
}
