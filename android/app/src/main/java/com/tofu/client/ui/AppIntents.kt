package com.tofu.client.ui

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.Toast
import com.tofu.client.R
import com.tofu.client.clientDiagnostics

/**
 * Open a URL in the system browser / handler. Used for the update download and
 * for outbound links the shell surfaces; failures are reported inline so a tap
 * is never a silent no-op.
 */
fun openUrl(context: Context, url: String) {
    try {
        context.startActivity(
            Intent(Intent.ACTION_VIEW, Uri.parse(url))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
    } catch (e: Exception) {
        context.clientDiagnostics()?.exception("ExternalIntent", e)
        Toast.makeText(context, context.getString(R.string.web_no_app), Toast.LENGTH_SHORT).show()
    }
}
