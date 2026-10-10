package com.tofu.client.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.res.stringResource
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.tofu.client.R
import com.tofu.client.clientDiagnostics
import androidx.compose.ui.platform.LocalContext

/**
 * The one QR-scan entry point, shared by the add form and first-run onboarding.
 *
 * Wraps ZXing's `ScanContract` (which itself supplies the capture Activity and
 * requests the camera permission) so callers stay declarative: call the
 * returned lambda to open the scanner; [onResult] receives decoded text only on
 * a successful scan (cancel delivers null and is ignored).
 */
@Composable
fun rememberQrScanner(onResult: (String) -> Unit): () -> Unit {
    val context = LocalContext.current
    val launcher = rememberLauncherForActivityResult(ScanContract()) { result ->
        context.clientDiagnostics()?.record("I", "QrScan", "completed=${result.contents != null}")
        result.contents?.let(onResult)
    }
    val prompt = stringResource(R.string.qr_prompt)
    return remember(launcher, prompt) {
        {
            context.clientDiagnostics()?.record("I", "QrScan", "open scanner")
            launcher.launch(
                ScanOptions()
                    .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                    .setPrompt(prompt)
                    .setBeepEnabled(false)
                    .setOrientationLocked(false),
            )
        }
    }
}
