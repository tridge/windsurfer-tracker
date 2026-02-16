# Volume Combo Assist Detection

The app detects simultaneous volume-up + volume-down press to toggle the assist
request. This is surprisingly difficult to implement reliably across Android
devices and screen states.

## Current Implementation (4 mechanisms)

### 1. AccessibilityService `onKeyEvent()` — VolumeKeyService

- Intercepts raw `KeyEvent` via `onKeyEvent()` before system processing
- Detects both keys pressed within 500ms window
- Returns `false` (non-consuming) so volume still changes normally
- Works when screen is on; `PhoneWindowManager` intercepts volume keys before
  the accessibility input pipeline when screen is off

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
- Works on Android 10; on Android 12+ `MediaSessionService` requires a
  `MediaRouter2` routing session and skips our session (`session=null`)

### 3. Silent AudioTrack on STREAM_MUSIC

- Plays a looping silent `AudioTrack` on `STREAM_MUSIC` using `MODE_STATIC`
  with `setLoopPoints(0, size, -1)` for zero CPU overhead
- Makes the system see "music is playing" so screen-off volume events
  (`musicOnly=true`) are passed through to `AudioService` instead of being
  dropped with "Nothing is playing on the music stream. Skipping volume event"
- Required for the VOLUME_CHANGED_ACTION broadcast to fire when screen is
  blanked on Android 12+ (without this, volume keys are silently dropped)

### 4. `VOLUME_CHANGED_ACTION` BroadcastReceiver

- Listens for `android.media.VOLUME_CHANGED_ACTION` broadcasts
- Extracts direction from `EXTRA_VOLUME_STREAM_VALUE` vs
  `EXTRA_PREV_VOLUME_STREAM_VALUE`
- Detects opposite directions within 800ms window
- Works on Android 12+ when screen is blanked (volume keys fall through to
  AudioService because our MediaSession is skipped, AudioService changes real
  volume and sends the broadcast)
- On Android 10 this broadcast doesn't fire when screen is off because the
  MediaSession VolumeProvider intercepts the keys before AudioService

### Debounce

All mechanisms can fire for the same combo, so `handleVolumeCombo()` has a
1-second debounce to prevent double-trigger.

### Assist Tones

Triple ascending (activate) or descending (deactivate) DTMF tones are generated
as a single PCM buffer and played through one `AudioTrack` on `STREAM_ALARM`.
This avoids gaps caused by `ToneGenerator.startTone()` latency when starting
each tone individually (which caused audible pauses between tones, especially
with screen off).

## Techniques That Don't Work

| Technique | Problem |
|-----------|---------|
| `ContentObserver` on `Settings.System.CONTENT_URI` | Volume management was moved out of `Settings.System` on newer Android; observer doesn't fire for volume changes |
| AccessibilityService `onKeyEvent()` with screen off | `PhoneWindowManager` handles volume keys before the accessibility input pipeline when screen is off; events never reach the service |
| `MediaSession` without `setCallback()` | `dispatchAdjustVolume()` posts to `CallbackMessageHandler` which is null without `setCallback()` — events silently dropped |
| `MediaSession` without `PlaybackState` | `MediaSessionService` skips sessions without a playback state when routing volume key events |
| `MediaSession` toggled inactive when screen on | On some devices the AccessibilityService doesn't reliably receive key events even with screen on, leaving no working detection path |
| `MediaSession` on Android 12+ without `MediaRouter2` | `MediaSessionRecord` requires a `MediaRouter2` routing session; without it `session=null` and `onAdjustVolume()` is never called |
| `VOLUME_CHANGED_ACTION` without silent AudioTrack | When screen is blanked, `PhoneWindowManager` dispatches volume keys with `musicOnly=true`; `MediaSessionService` checks if anything is playing on STREAM_MUSIC and skips the event entirely if not, so volume never changes and the broadcast never fires |
| `VOLUME_CHANGED_ACTION` on Android 14+ | Broadcast coalescing merges rapid volume broadcasts, losing the up/down pattern |
| `ToneGenerator.startTone()` for triple tones | Latency on each `startTone()` call causes audible pauses between tones; generate PCM buffer and play through single `AudioTrack` instead |

## Phone Test Results

| Label | Phone | OS | Screen On | Screen Off/Locked | Notes |
|-------|-------|----|-----------|-------------------|-------|
| Tracker1 | iPhone SE | iOS | Works | Works | Volume combo via iOS volume button detection. |
| Tracker5 | iPhone 7 | iOS | Works | Works | |
| Tracker9 | iPhone 7 | iOS | Works | Works | |
| Tracker2 | Pixel (1st gen) | 10 (API 29) | MediaSession | MediaSession | AccessibilityService `onKeyEvent()` works when screen on but `MediaSessionService` intercepts volume keys before accessibility when screen off. MediaSession always-active approach works in both states. |
| Tracker3 | Pixel 3a | 12 (API 31) | AccessibilityService | Broadcast + silent AudioTrack | MediaSession skipped on Android 12 (`No routing session for nz.co.tracker.windsurfer`, `session=null`). Without silent AudioTrack, screen-off volume events dropped (`Nothing is playing on the music stream. Skipping volume event`). With silent AudioTrack, volume keys pass through to AudioService and VOLUME_CHANGED_ACTION fires. |
| Tracker4 | Samsung Galaxy S10e | 12 (API 31) | Works | Works | All states working. |
| Tracker6 | Pixel 8a | 16 (API 36) | Works | Works | All states working. |
| Tracker7 | Pixel 5 | 14 (API 34) | Works | Works (needs slight pause) | Screen-blanked combo requires a brief pause between vol-up and vol-down, likely due to Android 14 broadcast coalescing merging rapid VOLUME_CHANGED_ACTION broadcasts. |
| Tracker8 | Pixel 5 | 14 (API 34) | Works | Works (needs slight pause) | Same as Tracker7 — Android 14 broadcast coalescing. |
| Tracker10 | Samsung Galaxy Z Flip3 | 13 (API 33) | Works | Works | All states working. |
| Tracker11 | Samsung Galaxy A53 | 13 (API 33) | Works | Works | All states working. |
| Tracker12 | Oppo CPH2069 | 11 (API 30) | Works | Works | ColorOS restricts ADB (`pm grant`, `settings put`) — setup needs manual steps. Auto-start on boot blocked by ColorOS Startup Manager (enable in Settings > App Management > Startup Manager). |
| Tracker13 | Samsung Galaxy A30 | 11 (API 30) | Works | Works | All states working. |
| Tracker14 | Samsung Galaxy A30 | 11 (API 30) | Works | Works | All states working. |

## Adding a New Phone

When testing on a new device:

1. Install the app, enable the VolumeKeyService accessibility service
2. Start tracking
3. Test volume combo with screen on — note if tones play
4. Lock screen (screen still visible), test volume combo — note if tones play
5. Blank screen (power button), test volume combo — note if tones play
6. Check `adb logcat | grep -iE "(VolumeKey|onAdjust|volume combo|VOLUME_CHANGED|MediaSession.*Adjust)"` for which mechanism fired
7. If broken with screen blanked, check for:
   - `"No routing session for nz.co.tracker.windsurfer"` — MediaSession skipped (Android 12+), needs broadcast+AudioTrack path
   - `"Nothing is playing on the music stream. Skipping volume event"` — silent AudioTrack not working
   - No log output at all — volume keys handled before reaching any of our mechanisms
8. Add results to the table above
