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

### Device Connectivity
- **WearOS**: Connect via `adb connect <ip>:<port>`
- **iOS/Android**: Enable developer mode, connect via USB or WiFi

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
| 6 | Toggle 1Hz Mode | Toggle works, setting persists |
| 7 | Select Event | Event list loads from server, can select |

#### 1.2 Version Display
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Scroll to bottom of Settings | Version string visible |
| 2 | Verify format | Shows: `X.Y.Z (build) githash` |
| 3 | Verify values | Version matches version.json (currently 1.9.7, build 37) |

#### 1.3 Heart Rate Setting (watchOS/WearOS only)
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
| 5 | Verify JSON fields | `id`, `ts`, `lat`/`lon` or `pos`, `spd`, `hdg`, `role`, `ver`, `os` present |

#### 2.2 Stop Tracking
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | While tracking, tap to stop | Tracking stops |
| 2 | Verify server | No more packets from this device |
| 3 | Check UI | Returns to config/stopped state |

#### 2.3 Authentication Error
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Enter wrong password | Start tracking |
| 2 | Check UI | Error message displayed |
| 3 | Fix password | Error clears on successful ACK |

---

### 3. Assist Feature

#### 3.1 Activate Assist
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | While tracking, press Assist button | Assist activates |
| 2 | Verify UI | Screen turns red/pulses, shows "ASSIST" |
| 3 | Check JSON packet | `"ast": true` in packets |
| 4 | Verify persistence | Subsequent packets still have `ast: true` |

#### 3.2 Cancel Assist
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | While assist active, press Cancel Assist | Assist deactivates |
| 2 | Verify UI | Screen returns to normal, shows "TRACKING" |
| 3 | Check JSON packet | `"ast": false` in packets |

#### 3.3 Assist Reset on Stop (Bug fix verification)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Activate assist | UI shows red |
| 2 | Stop tracking | Tracking stops |
| 3 | Start tracking again | UI shows normal (not red) |
| 4 | Check JSON packet | `"ast": false` (not persisted from before) |

---

### 4. 1Hz Mode

#### 4.1 Enable 1Hz Mode
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Enable 1Hz in settings | Setting saved |
| 2 | Start tracking | Tracking begins |
| 3 | Check JSON packets | `pos` array present (not `lat`/`lon`) |
| 4 | Verify array | 10 positions per packet: `[[ts, lat, lon], ...]` |

#### 4.2 1Hz Mode with Assist (Bug fix verification)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Enable 1Hz mode | Setting saved |
| 2 | Start tracking | Packets have `pos` array |
| 3 | Activate assist | UI shows red |
| 4 | Wait for next packet | `"ast": true` in packet |
| 5 | Verify persistence | ALL subsequent packets have `"ast": true` |

---

### 5. Heart Rate (watchOS/WearOS)

#### 5.1 Heart Rate Disabled (Default)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Verify HR toggle is OFF | Default state |
| 2 | Start tracking | Tracking begins |
| 3 | Check JSON packets | No `hr` field present |

#### 5.2 Heart Rate Enabled
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Enable Heart Rate in settings | Toggle ON |
| 2 | Start tracking | Tracking begins |
| 3 | Check JSON packets | `"hr": <number>` present |
| 4 | Verify value | Reasonable BPM (60-200 range) |

#### 5.3 Heart Rate in Simulator
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Enable HR in watchOS simulator | Toggle ON |
| 2 | Start tracking | Tracking begins |
| 3 | Check JSON packets | `"hr": 70-90` (simulated values) |

---

### 6. Background Tracking

#### 6.1 watchOS Background
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking | Tracking active |
| 2 | Press home button | App goes to background |
| 3 | Check server | Packets continue arriving |
| 4 | Note | Requires real device (simulator limitation) |

#### 6.2 WearOS Background
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking | Tracking active |
| 2 | Press home button | App goes to background |
| 3 | Check server | Packets continue arriving |

---

### 7. Status Display Updates

#### 7.1 ACK Rate Display
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking | Tracking begins |
| 2 | Wait for packets | ACK rate updates |
| 3 | Verify display | Shows percentage (e.g., "100%") |
| 4 | Verify color | Green (>80%), Yellow (50-80%), Red (<50%) |

#### 7.2 Packet Counts (watchOS)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start tracking | Tracking begins |
| 2 | Check status row | Shows "X/Y" (acked/sent) |
| 3 | Wait for packets | Numbers increment |

---

### 8. UI/UX Verification

#### 8.1 Settings Scroll (watchOS)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open Settings | Settings screen displays |
| 2 | Change Role (tap to cycle) | Role changes without blocking scroll |
| 3 | Scroll down to Save | Scrolling works smoothly |

#### 8.2 Gear Icon Position (watchOS)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | View tracking screen | Gear icon visible |
| 2 | Verify position | Top-left, not obscured by clock |

---

## Platform-Specific Notes

### watchOS Simulator Limitations
- Heart rate requires simulator workaround (fake values 70-90)
- Background tracking doesn't work (HKWorkoutSession fails)
- HealthKit authorization always succeeds (mocked)

### WearOS
- Heart rate default is OFF (opt-in for privacy)
- Requires BODY_SENSORS permission for heart rate

### iOS/Android Phone Apps
- Have more screen space for detailed status display
- Support landscape orientation

---

## Regression Checklist

After any code changes, verify:

- [ ] Tracking starts successfully
- [ ] Packets arrive at server with correct format
- [ ] Assist activates and shows in JSON as `ast: true`
- [ ] Assist persists in 1Hz mode packets
- [ ] Assist resets when tracking stops
- [ ] Heart rate appears when enabled (watchOS/WearOS)
- [ ] Version string shows correct version and git hash
- [ ] Settings persist after app restart
- [ ] Error messages display for auth failures
- [ ] Error clears after successful ACK
