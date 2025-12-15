package nz.co.tracker.windsurfer.presentation

import android.app.Activity
import android.app.RemoteInput
import android.content.Intent
import android.os.Bundle
import android.view.inputmethod.EditorInfo
import androidx.activity.ComponentActivity
import androidx.wear.input.RemoteInputIntentHelper

/**
 * Activity that launches the system text input (voice or keyboard) for Wear OS.
 */
class TextInputActivity : ComponentActivity() {

    companion object {
        const val EXTRA_INPUT_TYPE = "input_type"
        const val EXTRA_LABEL = "label"
        const val EXTRA_CURRENT_VALUE = "current_value"
        const val RESULT_TEXT = "result_text"

        const val INPUT_TYPE_TEXT = 0
        const val INPUT_TYPE_PASSWORD = 1

        private const val REQUEST_CODE_INPUT = 1001
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val label = intent.getStringExtra(EXTRA_LABEL) ?: "Enter text"
        val currentValue = intent.getStringExtra(EXTRA_CURRENT_VALUE) ?: ""
        val inputType = intent.getIntExtra(EXTRA_INPUT_TYPE, INPUT_TYPE_TEXT)

        // Create RemoteInput for text entry
        val remoteInputs = listOf(
            RemoteInput.Builder(RESULT_TEXT)
                .setLabel(label)
                .build()
        )

        // Create intent for system text input
        val inputIntent = RemoteInputIntentHelper.createActionRemoteInputIntent()
        RemoteInputIntentHelper.putRemoteInputsExtra(inputIntent, remoteInputs)

        // Set input type hints
        if (inputType == INPUT_TYPE_PASSWORD) {
            // For password, prefer keyboard over voice
            val wearableExtras = Bundle().apply {
                putInt("android.support.wearable.input.EXTRA_INPUT_TYPE", EditorInfo.TYPE_TEXT_VARIATION_PASSWORD)
            }
            inputIntent.putExtras(wearableExtras)
        }

        startActivityForResult(inputIntent, REQUEST_CODE_INPUT)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)

        if (requestCode == REQUEST_CODE_INPUT) {
            if (resultCode == Activity.RESULT_OK && data != null) {
                val results = RemoteInput.getResultsFromIntent(data)
                val text = results?.getCharSequence(RESULT_TEXT)?.toString() ?: ""

                val resultIntent = Intent().apply {
                    putExtra(RESULT_TEXT, text)
                }
                setResult(Activity.RESULT_OK, resultIntent)
            } else {
                setResult(Activity.RESULT_CANCELED)
            }
            finish()
        }
    }
}
