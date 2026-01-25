package nz.co.tracker.windsurfer.presentation

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.material.TextField
import androidx.compose.material.TextFieldDefaults
import androidx.wear.compose.material.*

/**
 * Activity that provides text input with pre-filled value for Wear OS.
 */
class TextInputActivity : ComponentActivity() {

    companion object {
        const val EXTRA_INPUT_TYPE = "input_type"
        const val EXTRA_LABEL = "label"
        const val EXTRA_CURRENT_VALUE = "current_value"
        const val RESULT_TEXT = "result_text"

        const val INPUT_TYPE_TEXT = 0
        const val INPUT_TYPE_PASSWORD = 1
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val label = intent.getStringExtra(EXTRA_LABEL) ?: "Enter text"
        val currentValue = intent.getStringExtra(EXTRA_CURRENT_VALUE) ?: ""
        val inputType = intent.getIntExtra(EXTRA_INPUT_TYPE, INPUT_TYPE_TEXT)

        setContent {
            MaterialTheme {
                TextInputScreen(
                    label = label,
                    initialValue = currentValue,
                    isPassword = inputType == INPUT_TYPE_PASSWORD,
                    onConfirm = { text ->
                        val resultIntent = Intent().apply {
                            putExtra(RESULT_TEXT, text)
                        }
                        setResult(Activity.RESULT_OK, resultIntent)
                        finish()
                    },
                    onCancel = {
                        setResult(Activity.RESULT_CANCELED)
                        finish()
                    }
                )
            }
        }
    }
}

@Composable
fun TextInputScreen(
    label: String,
    initialValue: String,
    isPassword: Boolean,
    onConfirm: (String) -> Unit,
    onCancel: () -> Unit
) {
    var text by remember { mutableStateOf(initialValue) }
    val focusRequester = remember { FocusRequester() }

    Scaffold(
        timeText = { TimeText() }
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(horizontal = 16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center
        ) {
            Text(
                text = label,
                style = MaterialTheme.typography.caption1,
                color = MaterialTheme.colors.onSurfaceVariant
            )

            Spacer(modifier = Modifier.height(8.dp))

            TextField(
                value = text,
                onValueChange = { text = it },
                modifier = Modifier
                    .fillMaxWidth()
                    .focusRequester(focusRequester),
                singleLine = true,
                visualTransformation = VisualTransformation.None,
                keyboardOptions = KeyboardOptions(
                    keyboardType = KeyboardType.Text,
                    imeAction = ImeAction.Done
                ),
                keyboardActions = KeyboardActions(
                    onDone = { onConfirm(text) }
                ),
                colors = TextFieldDefaults.textFieldColors(
                    textColor = Color.White,
                    backgroundColor = Color.DarkGray,
                    cursorColor = Color.White
                )
            )

            Spacer(modifier = Modifier.height(12.dp))

            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Button(
                    onClick = onCancel,
                    colors = ButtonDefaults.secondaryButtonColors(),
                    modifier = Modifier.size(ButtonDefaults.SmallButtonSize)
                ) {
                    Text("✕")
                }

                Button(
                    onClick = { onConfirm(text) },
                    colors = ButtonDefaults.primaryButtonColors(),
                    modifier = Modifier.size(ButtonDefaults.SmallButtonSize)
                ) {
                    Text("✓")
                }
            }
        }
    }

    LaunchedEffect(Unit) {
        focusRequester.requestFocus()
    }
}
