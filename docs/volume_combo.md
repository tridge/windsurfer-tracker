# Volume Combo Assist Detection

The app detects simultaneous volume-up + volume-down press to toggle the assist
request. This is surprisingly difficult to implement reliably across Android
devices and screen states.

## Current Implementation (2 mechanisms)

### 1. AccessibilityService `onKeyEvent()` — VolumeKeyService

- Intercepts raw `KeyEvent` via `onKeyEvent()` before system processing
- Detects both keys pressed within 500ms window
- Returns `false` (non-consuming) so volume still changes normally

### 2. MediaSession `VolumeProvider.onAdjustVolume()` — TrackerService

- Creates a `MediaSession` with `setPlaybackToRemote(VolumeProvider)` and
  `STATE_PLAYING` playback state so `MediaSessionService` routes volume keys
  to our callback
- `onAdjustVolume(direction)` receives +1 (up), -1 (down), 0 (key release)
- Detects opposite directions within 800ms window
- Forwards volume changes to `AudioManager.adjustStreamVolume()` so volume
  still works normally
- Requires `setCallback()` with an empty `MediaSession.Callback` — without
  this, the internal `CallbackMessageHandler` is null and all events are
  silently dropped (Android framework bug/design)
- Session kept active at all times (not toggled by screen state)

### Debounce

Both mechanisms can fire for the same combo, so `handleVolumeCombo()` has a
1-second debounce to prevent double-trigger.

## Techniques That Don't Work

| Technique | Problem |
|-----------|---------|
| `BroadcastReceiver` for `VOLUME_CHANGED_ACTION` | Android 14+ broadcast coalescing merges rapid volume broadcasts, losing the up/down pattern |
| `ContentObserver` on `Settings.System.CONTENT_URI` | Volume management was moved out of `Settings.System` on newer Android; observer doesn't fire for volume changes |
| AccessibilityService `onKeyEvent()` with screen off | `PhoneWindowManager` handles volume keys before the accessibility input pipeline when screen is off; events never reach the service |
| `MediaSession` without `setCallback()` | `dispatchAdjustVolume()` posts to `CallbackMessageHandler` which is null without `setCallback()` — events silently dropped |
| `MediaSession` without `PlaybackState` | `MediaSessionService` skips sessions without a playback state when routing volume key events |
| `MediaSession` toggled inactive when screen on | On some devices the AccessibilityService doesn't reliably receive key events even with screen on, leaving no working detection path |

## Phone Test Results

| Phone | Android | Screen On | Screen Off/Locked | Notes |
|-------|---------|-----------|-------------------|-------|
| Pixel (1st gen) | 10 (API 29) | MediaSession | MediaSession | AccessibilityService `onKeyEvent()` works when screen on but `MediaSessionService` intercepts volume keys before accessibility when screen off. MediaSession always-active approach works in both states. |

## Adding a New Phone

When testing on a new device:

1. Install the app, enable the VolumeKeyService accessibility service
2. Start tracking
3. Test volume combo with screen on — note if tones play
4. Lock screen, test volume combo — note if tones play
5. Check `adb logcat | grep -iE "(VolumeKey|onAdjust|volume combo|MediaSession.*Adjust)"` for which mechanism fired
6. If broken, check if `MediaSessionService` logs `Adjusting nz.co.tracker.windsurfer/WindsurferAssist` — if not, the session may not be the highest priority (another media app active?)
7. Add results to the table above
