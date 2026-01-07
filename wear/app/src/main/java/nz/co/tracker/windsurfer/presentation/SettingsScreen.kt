package nz.co.tracker.windsurfer.presentation

import android.app.Activity
import android.content.Intent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.focusable
import androidx.compose.foundation.layout.*
import androidx.compose.runtime.*
import kotlinx.coroutines.delay
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.input.rotary.onRotaryScrollEvent
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.foundation.lazy.rememberScalingLazyListState
import androidx.wear.compose.material.*
import kotlinx.coroutines.launch
import nz.co.tracker.windsurfer.BuildConfig
import nz.co.tracker.windsurfer.EventFetcher
import nz.co.tracker.windsurfer.EventInfo
import nz.co.tracker.windsurfer.TrackerService
import nz.co.tracker.windsurfer.TrackerSettings

@Composable
fun SettingsScreen(
    settings: TrackerSettings,
    onSave: (TrackerSettings) -> Unit,
    onBack: () -> Unit,
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current

    var serverHost by remember { mutableStateOf(settings.serverHost) }
    var sailorId by remember { mutableStateOf(settings.sailorId) }
    var password by remember { mutableStateOf(settings.password) }
    var highFrequencyMode by remember { mutableStateOf(settings.highFrequencyMode) }
    var heartRateEnabled by remember { mutableStateOf(settings.heartRateEnabled) }
    var trackerBeep by remember { mutableStateOf(settings.trackerBeep) }
    var validationError by remember { mutableStateOf<String?>(null) }

    // Track original auth values to detect changes
    val originalSailorId = remember { settings.sailorId }
    val originalPassword = remember { settings.password }
    val originalEventId = remember { settings.eventId }

    var selectedRoleIndex by remember {
        mutableStateOf(
            when (settings.role) {
                "sailor" -> 0
                "support" -> 1
                "spectator" -> 2
                else -> 0
            }
        )
    }
    var selectedEventId by remember { mutableStateOf(settings.eventId) }
    var events by remember { mutableStateOf<List<EventInfo>>(emptyList()) }
    var eventsLoading by remember { mutableStateOf(true) }
    var isCheckingPassword by remember { mutableStateOf(false) }

    val roles = listOf("sailor", "support", "spectator")
    val eventFetcher = remember { EventFetcher() }
    val coroutineScope = rememberCoroutineScope()

    // Fetch events when screen loads or server changes
    LaunchedEffect(serverHost) {
        eventsLoading = true
        events = eventFetcher.fetchEvents(serverHost, TrackerService.DEFAULT_SERVER_PORT)
        eventsLoading = false
        // Ensure selected event exists in list
        if (events.isNotEmpty() && events.none { it.eid == selectedEventId }) {
            selectedEventId = events.first().eid
        }
    }

    // Launcher for server input
    val serverInputLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            result.data?.getStringExtra(TextInputActivity.RESULT_TEXT)?.let {
                if (it.isNotBlank()) serverHost = it
            }
        }
    }

    // Launcher for ID input
    val idInputLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            result.data?.getStringExtra(TextInputActivity.RESULT_TEXT)?.let {
                if (it.isNotBlank()) sailorId = it
            }
        }
    }

    // Launcher for password input
    val passwordInputLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            result.data?.getStringExtra(TextInputActivity.RESULT_TEXT)?.let {
                password = it  // Allow empty password to clear it
            }
        }
    }

    val listState = rememberScalingLazyListState()
    val focusRequester = remember { FocusRequester() }

    Scaffold(
        timeText = { TimeText() },
        vignette = { Vignette(vignettePosition = VignettePosition.TopAndBottom) },
        positionIndicator = { PositionIndicator(scalingLazyListState = listState) }
    ) {
        ScalingLazyColumn(
            state = listState,
            modifier = modifier
                .fillMaxSize()
                .onRotaryScrollEvent { true }
                .focusRequester(focusRequester)
                .focusable(),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(4.dp)
        ) {
            item {
                Text(
                    text = "Settings",
                    style = MaterialTheme.typography.title2,
                    modifier = Modifier.padding(bottom = 8.dp)
                )
            }

            // Your Name
            item {
                Text(
                    text = "Your Name",
                    style = MaterialTheme.typography.caption1,
                    color = MaterialTheme.colors.onSurfaceVariant,
                    modifier = Modifier.padding(top = 8.dp)
                )
            }

            item {
                Chip(
                    onClick = {
                        val intent = Intent(context, TextInputActivity::class.java).apply {
                            putExtra(TextInputActivity.EXTRA_LABEL, "Your Name")
                            putExtra(TextInputActivity.EXTRA_CURRENT_VALUE, sailorId)
                            putExtra(TextInputActivity.EXTRA_INPUT_TYPE, TextInputActivity.INPUT_TYPE_TEXT)
                        }
                        idInputLauncher.launch(intent)
                    },
                    label = {
                        Text(
                            text = sailorId,
                            fontSize = 14.sp
                        )
                    },
                    modifier = Modifier.fillMaxWidth(0.9f),
                    colors = ChipDefaults.secondaryChipColors()
                )
            }

            // Password
            item {
                Text(
                    text = "Password",
                    style = MaterialTheme.typography.caption1,
                    color = MaterialTheme.colors.onSurfaceVariant,
                    modifier = Modifier.padding(top = 8.dp)
                )
            }

            item {
                Chip(
                    onClick = {
                        val intent = Intent(context, TextInputActivity::class.java).apply {
                            putExtra(TextInputActivity.EXTRA_LABEL, "Password")
                            putExtra(TextInputActivity.EXTRA_CURRENT_VALUE, password)
                            putExtra(TextInputActivity.EXTRA_INPUT_TYPE, TextInputActivity.INPUT_TYPE_PASSWORD)
                        }
                        passwordInputLauncher.launch(intent)
                    },
                    label = {
                        Text(
                            text = if (password.isEmpty()) "(tap to set)" else password,
                            maxLines = 1,
                            fontSize = 12.sp
                        )
                    },
                    modifier = Modifier.fillMaxWidth(0.9f),
                    colors = ChipDefaults.secondaryChipColors()
                )
            }

            // Event selector
            item {
                Text(
                    text = "Event",
                    style = MaterialTheme.typography.caption1,
                    color = MaterialTheme.colors.onSurfaceVariant,
                    modifier = Modifier.padding(top = 8.dp)
                )
            }

            item {
                if (eventsLoading) {
                    Chip(
                        onClick = { },
                        label = { Text("Loading...", fontSize = 12.sp) },
                        modifier = Modifier.fillMaxWidth(0.9f),
                        colors = ChipDefaults.secondaryChipColors()
                    )
                } else if (events.isEmpty()) {
                    Chip(
                        onClick = { },
                        label = { Text("Event ID: $selectedEventId", fontSize = 12.sp) },
                        modifier = Modifier.fillMaxWidth(0.9f),
                        colors = ChipDefaults.secondaryChipColors()
                    )
                } else {
                    // Show current event with ability to cycle through
                    val currentEvent = events.find { it.eid == selectedEventId } ?: events.first()
                    Chip(
                        onClick = {
                            // Cycle to next event
                            val currentIndex = events.indexOfFirst { it.eid == selectedEventId }
                            val nextIndex = (currentIndex + 1) % events.size
                            selectedEventId = events[nextIndex].eid
                        },
                        label = {
                            Text(
                                text = currentEvent.name,
                                maxLines = 1,
                                fontSize = 11.sp
                            )
                        },
                        secondaryLabel = {
                            Text("Tap to change", fontSize = 9.sp)
                        },
                        modifier = Modifier.fillMaxWidth(0.9f),
                        colors = ChipDefaults.secondaryChipColors()
                    )
                }
            }

            // 1Hz Mode Toggle
            item {
                ToggleChip(
                    checked = highFrequencyMode,
                    onCheckedChange = { highFrequencyMode = it },
                    label = { Text("1Hz Race Mode") },
                    secondaryLabel = { Text(if (highFrequencyMode) "On" else "Off", fontSize = 10.sp) },
                    toggleControl = {
                        Switch(checked = highFrequencyMode)
                    },
                    modifier = Modifier
                        .fillMaxWidth(0.9f)
                        .padding(top = 8.dp)
                )
            }

            // Heart Rate Toggle
            item {
                ToggleChip(
                    checked = heartRateEnabled,
                    onCheckedChange = { heartRateEnabled = it },
                    label = { Text("Heart Rate") },
                    secondaryLabel = { Text(if (heartRateEnabled) "On" else "Off", fontSize = 10.sp) },
                    toggleControl = {
                        Switch(checked = heartRateEnabled)
                    },
                    modifier = Modifier
                        .fillMaxWidth(0.9f)
                        .padding(top = 4.dp)
                )
            }

            // Tracking Buzz Toggle
            item {
                ToggleChip(
                    checked = trackerBeep,
                    onCheckedChange = { trackerBeep = it },
                    label = { Text("Tracking Buzz") },
                    secondaryLabel = { Text(if (trackerBeep) "On" else "Off", fontSize = 10.sp) },
                    toggleControl = {
                        Switch(checked = trackerBeep)
                    },
                    modifier = Modifier
                        .fillMaxWidth(0.9f)
                        .padding(top = 4.dp)
                )
            }

            // Role
            item {
                Text(
                    text = "Role",
                    style = MaterialTheme.typography.caption1,
                    color = MaterialTheme.colors.onSurfaceVariant,
                    modifier = Modifier.padding(top = 8.dp)
                )
            }

            item {
                ToggleChip(
                    checked = selectedRoleIndex == 0,
                    onCheckedChange = { if (it) selectedRoleIndex = 0 },
                    label = { Text("Sailor") },
                    toggleControl = {
                        RadioButton(selected = selectedRoleIndex == 0)
                    },
                    modifier = Modifier.fillMaxWidth(0.9f)
                )
            }

            item {
                ToggleChip(
                    checked = selectedRoleIndex == 1,
                    onCheckedChange = { if (it) selectedRoleIndex = 1 },
                    label = { Text("Support") },
                    toggleControl = {
                        RadioButton(selected = selectedRoleIndex == 1)
                    },
                    modifier = Modifier.fillMaxWidth(0.9f)
                )
            }

            item {
                ToggleChip(
                    checked = selectedRoleIndex == 2,
                    onCheckedChange = { if (it) selectedRoleIndex = 2 },
                    label = { Text("Spectator") },
                    toggleControl = {
                        RadioButton(selected = selectedRoleIndex == 2)
                    },
                    modifier = Modifier.fillMaxWidth(0.9f)
                )
            }

            // Server Host
            item {
                Text(
                    text = "Server",
                    style = MaterialTheme.typography.caption1,
                    color = MaterialTheme.colors.onSurfaceVariant,
                    modifier = Modifier.padding(top = 8.dp)
                )
            }

            item {
                Chip(
                    onClick = {
                        val intent = Intent(context, TextInputActivity::class.java).apply {
                            putExtra(TextInputActivity.EXTRA_LABEL, "Server address")
                            putExtra(TextInputActivity.EXTRA_CURRENT_VALUE, serverHost)
                            putExtra(TextInputActivity.EXTRA_INPUT_TYPE, TextInputActivity.INPUT_TYPE_TEXT)
                        }
                        serverInputLauncher.launch(intent)
                    },
                    label = {
                        Text(
                            text = serverHost,
                            maxLines = 1,
                            fontSize = 12.sp
                        )
                    },
                    modifier = Modifier.fillMaxWidth(0.9f),
                    colors = ChipDefaults.secondaryChipColors()
                )
            }

            // Validation error
            if (validationError != null) {
                item {
                    Text(
                        text = validationError!!,
                        style = MaterialTheme.typography.caption2,
                        color = MaterialTheme.colors.error,
                        textAlign = TextAlign.Center,
                        modifier = Modifier.padding(top = 8.dp)
                    )
                }
            }

            // Save button
            item {
                Button(
                    onClick = {
                        // Validate required fields
                        when {
                            sailorId.isEmpty() && password.isEmpty() -> {
                                validationError = "Name and password required"
                                return@Button
                            }
                            sailorId.isEmpty() -> {
                                validationError = "Name is required"
                                return@Button
                            }
                            password.isEmpty() -> {
                                validationError = "Password is required"
                                return@Button
                            }
                        }

                        // Check if auth fields changed
                        val authFieldsChanged = sailorId != originalSailorId ||
                            password != originalPassword ||
                            selectedEventId != originalEventId

                        // Function to save and exit
                        fun doSave() {
                            validationError = null
                            onSave(
                                TrackerSettings(
                                    serverHost = serverHost,
                                    sailorId = sailorId,
                                    role = roles[selectedRoleIndex],
                                    password = password,
                                    eventId = selectedEventId,
                                    highFrequencyMode = highFrequencyMode,
                                    heartRateEnabled = heartRateEnabled,
                                    trackerBeep = trackerBeep
                                )
                            )
                            onBack()
                        }

                        if (authFieldsChanged) {
                            // Check password with server
                            isCheckingPassword = true
                            validationError = "Checking password..."
                            coroutineScope.launch {
                                val osVersion = "WearOS ${android.os.Build.VERSION.RELEASE}"
                                val result = eventFetcher.checkPassword(
                                    serverHost,
                                    TrackerService.DEFAULT_SERVER_PORT,
                                    selectedEventId,
                                    password,
                                    userId = sailorId,
                                    userOs = osVersion,
                                    userVer = BuildConfig.VERSION_STRING
                                )
                                isCheckingPassword = false

                                if (result.isFailure) {
                                    validationError = result.exceptionOrNull()?.message ?: "Incorrect password"
                                    return@launch
                                }

                                doSave()
                            }
                        } else {
                            // No auth fields changed, save directly
                            doSave()
                        }
                    },
                    enabled = !isCheckingPassword,
                    modifier = Modifier
                        .fillMaxWidth(0.7f)
                        .padding(top = 12.dp),
                    colors = ButtonDefaults.primaryButtonColors()
                ) {
                    Text(if (isCheckingPassword) "..." else "Save")
                }
            }

            // Version info
            item {
                Text(
                    text = BuildConfig.VERSION_STRING,
                    style = MaterialTheme.typography.caption3,
                    color = MaterialTheme.colors.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(top = 16.dp, bottom = 8.dp)
                )
            }
        }

        LaunchedEffect(Unit) {
            focusRequester.requestFocus()
        }
    }
}
