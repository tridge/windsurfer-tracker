# Windsurfer Tracker Test Plan

This document outlines testing procedures for the Windsurfer Tracker apps across all platforms.

## Platforms

- **watchOS** (Apple Watch) - Swift/SwiftUI
- **WearOS** (Pixel Watch, etc.) - Kotlin/Compose
- **iOS** (iPhone/iPad) - Swift/SwiftUI
- **Android** (Phone) - Kotlin

---

## Pre-Test Setup

### Server
1. Ensure tracker server is running and accessible
2. Verify server password is known for testing
3. Have WebUI open to monitor incoming packets
4. Configure an event with idle mode enabled (idle_interval > 0)

### Device Connectivity
- **WearOS**: Connect via `adb connect <ip>:<port>`
- **iOS/Android**: Enable developer mode, connect via USB or WiFi

### Server Tests
Run the automated test suite before manual testing:
```bash
cd test && python -m pytest -x
```
All 110+ tests should pass.

---

## Test Cases

### 1. Settings Configuration

#### 1.1 Basic Settings
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open Settings | Settings screen displays |
| 2 | Enter Name/ID | Text input works, saves on exit |
| 3 | Change Role | Can cycle through Sailor/Support/Spectator |
| 4 | Enter Server address | Accepts hostname or IP |
| 5 | Enter Password | Password field accepts input |
| 6 | Select Event | Event list loads from server, can select |
| 7 | Switch events | Password cached per-event, restored on switch |

#### 1.2 Version Display
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Scroll to bottom of Settings | Version string visible |
| 2 | Verify format | Shows: `X.Y.Z (build) githash` |

#### 1.3 Tracker Beep
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Find Tracker Beep toggle | Toggle present, ON by default |
| 2 | Start tracking, wait 60s | Single vibration buzz (connected) |
| 3 | Disable WiFi/data, wait 60s | Double vibration buzz (no connection) |

#### 1.4 Heart Rate Setting (watchOS/WearOS only)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Find Heart Rate toggle | Toggle present in settings |
| 2 | Verify default | Should be OFF by default |
| 3 | Toggle ON | Setting persists |

---

### 2. Start/Stop Tracking

#### 2.1 Start Tracking
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Configure name and password | Fields populated |
| 2 | Press Start | Tracking begins |
| 3 | Verify location permission | Prompted if not granted |
| 4 | Check server | Packets appearing on server |
| 5 | Verify JSON fields | `id`, `ts`, `pos` array, `spd`, `hdg`, `role`, `ver`, `os`, `nsats` present |

#### 2.2 GPS Wait State
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking indoors (poor GPS) | Status shows "GPS wait" |
| 2 | Check server within 10s | Heartbeat packets arriving with `nsats: 0`, no `lat`/`lon` |
| 3 | Check WebUI | Device shows with "NOGPS" badge |
| 4 | Move outdoors / get GPS fix | Status progresses to "connecting..." then event name |

#### 2.3 Stop Tracking
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | While tracking, tap Stop | Confirmation dialog shown |
| 2 | Confirm stop | Stop packet sent with `stopped: true` |
| 3 | Check server | Position marked as stopped |
| 4 | If idle enabled | App enters idle mode (not full stop) |

#### 2.4 Authentication Error
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Enter wrong password | Start tracking |
| 2 | Check UI | "auth failure" status displayed |
| 3 | Fix password in settings | Error clears on successful ACK |

---

### 3. Idle Mode

#### 3.1 Enter Idle Mode
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking with idle-enabled event | Tracking active |
| 2 | Stop tracking | App enters idle mode (not full stop) |
| 3 | Check notification | Shows "Idle - waiting for admin start" |
| 4 | Check server | Idle heartbeat packets arriving with `idle: true` |
| 5 | Check WebUI | Device shows with "IDLE" badge, battery and signal visible |

#### 3.2 Remote Start from Idle
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Device in idle mode | Idle heartbeats arriving |
| 2 | Click Start in WebUI admin | Server sends `start` command |
| 3 | Check device | Transitions to tracking (GPS wait → connected) |

#### 3.3 Remote Shutdown from Idle
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Device in idle mode | Idle heartbeats arriving |
| 2 | Click Shutdown in WebUI admin | Server sends `shutdown` command |
| 3 | Check device | Service stops, returns to config screen |

#### 3.4 Boot-to-Idle (Android only)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Configure tracking, stop to enter idle | Idle mode active |
| 2 | Reboot device | Device reboots |
| 3 | Check server after boot (~30s) | Idle heartbeats resume |
| 4 | Send start command from WebUI | Tracking starts |

---

### 4. Network Resilience

#### 4.1 WiFi Toggle (Android)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking or enter idle on WiFi | Packets flowing |
| 2 | `adb shell svc wifi disable` | "Network lost" in logcat, socket closed |
| 3 | Wait for cellular | "Network available" in logcat |
| 4 | Check server | Packets resume within one interval |
| 5 | `adb shell svc wifi enable` | Another transition, packets continue |

#### 4.2 Airplane Mode Recovery
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Device in idle or tracking | Packets flowing |
| 2 | Enable airplane mode | Sends fail (logged) |
| 3 | Disable airplane mode | Packets resume after network available |

#### 4.3 DNS Fallback
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Reboot device (Android boot-to-idle) | Service starts before network |
| 2 | Check logcat | "DNS failed, using hardcoded fallback" for wstracker.org |
| 3 | Once network up | DNS resolves normally, packets flow |

---

### 5. Remote Commands

#### 5.1 Remote Stop
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Device tracking | Packets flowing |
| 2 | Click Stop in WebUI admin panel | Server sends stop command |
| 3 | Check device | Enters idle mode (or stops if idle disabled) |

#### 5.2 Remote Cancel Assist
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Activate assist on device | Assist active, `ast: true` |
| 2 | Click Cancel Assist in WebUI | Server sends cancel_assist command |
| 3 | Check device | Assist cleared, UI returns to normal |

---

### 6. Assist Feature

#### 6.1 Activate Assist
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | While tracking, long-press Assist button | Assist activates |
| 2 | Verify UI | Button turns red, pulses, shows "ASSIST" |
| 3 | Check JSON packet | `"ast": true` in packets |
| 4 | Verify persistence | Subsequent packets still have `ast: true` |

#### 6.2 Cancel Assist
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | While assist active, long-press Cancel | Assist deactivates |
| 2 | Verify UI | Button returns to green |
| 3 | Check JSON packet | `"ast": false` in packets |

#### 6.3 Assist Reset on Stop
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Activate assist | UI shows red |
| 2 | Stop tracking | Tracking stops |
| 3 | Start tracking again | UI shows normal (not red) |
| 4 | Check JSON packet | `"ast": false` (not persisted from before) |

---

### 7. 1Hz Mode (Always On)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking | Tracking begins |
| 2 | Wait 10 seconds | First batch packet sent |
| 3 | Check JSON packets | `pos` array present with ~10 entries |
| 4 | Verify array format | `[[ts, lat, lon, spd], ...]` (4 values per entry) |

---

### 8. Background Tracking

#### 8.1 watchOS Background
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking | Tracking active |
| 2 | Press home button | App goes to background |
| 3 | Check server | Packets continue arriving |
| 4 | Note | Requires real device (simulator limitation) |

#### 8.2 WearOS Background
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking | Tracking active |
| 2 | Press home button | App goes to background |
| 3 | Check server | Packets continue arriving |

#### 8.3 Android Auto-Start on Boot
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Have tracking active, reboot device | Device reboots |
| 2 | Check server | Idle heartbeats appear after boot |
| 3 | Lock screen | Shows "IDLE" notification |

---

### 9. Status Display Updates

#### 9.1 ACK Rate Display
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking | Tracking begins |
| 2 | Wait for packets | ACK rate updates |
| 3 | Verify display | Shows percentage (e.g., "100%") |
| 4 | Verify color | Green (>80%), Orange (50-80%), Red (<50%) |

#### 9.2 WebUI Sidebar
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Multiple devices tracking | Sidebar lists all devices |
| 2 | Verify badges | IDLE (blue), NOGPS (orange), STOP (red) as appropriate |
| 3 | Charging device | Green lightning bolt shown next to battery |
| 4 | Device with power saver | Orange warning badge shown |

---

### 10. UI/UX Verification

#### 10.1 Settings Scroll (watchOS)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open Settings | Settings screen displays |
| 2 | Change Role (tap to cycle) | Role changes without blocking scroll |
| 3 | Scroll down to Save | Scrolling works smoothly |

#### 10.2 Live Tracking Link
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | View config screen | Live tracking URL shown |
| 2 | Tap URL | Opens browser to event page |

---

## Platform-Specific Notes

### watchOS Simulator Limitations
- Heart rate requires simulator workaround (fake values 70-90)
- Background tracking doesn't work (HKWorkoutSession fails)
- HealthKit authorization always succeeds (mocked)

### WearOS
- Heart rate default is OFF (opt-in for privacy)
- Requires BODY_SENSORS permission for heart rate
- Ongoing activity notification for ambient mode

### iOS
- Live Activities on lock screen (iOS 16.1+)
- HealthKit workout logging

### Android
- Auto-start on boot via Direct Boot
- Network resilience via ConnectivityManager callback
- Custom notification icons (OK vs error state)

---

## Regression Checklist

After any code changes, verify:

- [ ] Tracking starts successfully
- [ ] Packets arrive at server with correct format (pos array, nsats)
- [ ] GPS-wait heartbeats sent when no GPS fix
- [ ] Assist activates and shows in JSON as `ast: true`
- [ ] Assist persists across packets
- [ ] Assist resets when tracking stops
- [ ] Stop packet sent with `stopped: true`
- [ ] Idle mode entered after stop (when enabled)
- [ ] Idle heartbeats arrive at server
- [ ] Remote start/stop/shutdown commands work from WebUI
- [ ] Network change recovery works (WiFi toggle test)
- [ ] Heart rate appears when enabled (watchOS/WearOS)
- [ ] Version string shows correct version and git hash
- [ ] Settings persist after app restart
- [ ] Error messages display for auth failures
- [ ] Error clears after successful ACK
- [ ] Server tests pass: `cd test && python -m pytest -x`
