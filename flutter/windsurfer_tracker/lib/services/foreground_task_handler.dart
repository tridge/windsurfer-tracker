import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';
import 'package:flutter/foundation.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:geolocator/geolocator.dart';
import 'package:geolocator_apple/geolocator_apple.dart';
import 'package:battery_plus/battery_plus.dart';

/// Configuration constants
class TrackerConfig {
  static const String defaultServerHost = 'wstracker.org';
  static const int defaultServerPort = 41234;
  static const Duration locationInterval = Duration(seconds: 10);
  static const int udpRetryCount = 3;
  static const Duration udpRetryDelay = Duration(milliseconds: 1500);
  static const Duration dnsRefreshInterval = Duration(minutes: 5);
  // Accuracy filtering: reject locations with accuracy worse than this (meters)
  // 0 = disabled. OwnTracks uses similar filtering.
  static const double maxAccuracyMeters = 100.0;
}

/// Initialize the foreground task
Future<void> initForegroundTask() async {
  FlutterForegroundTask.init(
    androidNotificationOptions: AndroidNotificationOptions(
      channelId: 'windsurfer_tracker',
      channelName: 'Windsurfer Tracker',
      channelDescription: 'GPS tracking notification',
      channelImportance: NotificationChannelImportance.LOW,
      priority: NotificationPriority.LOW,
    ),
    iosNotificationOptions: const IOSNotificationOptions(
      showNotification: true,
      playSound: false,
    ),
    foregroundTaskOptions: ForegroundTaskOptions(
      eventAction: ForegroundTaskEventAction.repeat(10000), // 10 seconds
      autoRunOnBoot: true, // Auto-restart tracking after device reboot
      autoRunOnMyPackageReplaced: true, // Auto-restart after app update
      allowWakeLock: true,
      allowWifiLock: true,
    ),
  );
}

/// Start the foreground tracking service
Future<ServiceRequestResult> startForegroundTracking({
  required String serverHost,
  required int serverPort,
  required String sailorId,
  required String role,
  required String password,
}) async {
  return FlutterForegroundTask.startService(
    notificationTitle: 'Windsurfer Tracker',
    notificationText: 'Tracking active - $sailorId',
    notificationIcon: null,
    callback: startCallback,
    notificationInitialRoute: '/',
  );
}

/// Stop the foreground tracking service
Future<ServiceRequestResult> stopForegroundTracking() async {
  return FlutterForegroundTask.stopService();
}

/// Send configuration to the task
void sendConfigToTask({
  required String serverHost,
  required int serverPort,
  required String sailorId,
  required String role,
  required String password,
  required int eventId,
  required String version,
  required bool highFrequencyMode,
}) {
  FlutterForegroundTask.sendDataToTask({
    'type': 'config',
    'serverHost': serverHost,
    'serverPort': serverPort,
    'sailorId': sailorId,
    'role': role,
    'password': password,
    'eventId': eventId,
    'version': version,
    'highFrequencyMode': highFrequencyMode,
  });
}

/// Send assist request to the task
void sendAssistToTask(bool enabled) {
  FlutterForegroundTask.sendDataToTask({
    'type': 'assist',
    'enabled': enabled,
  });
}

/// Callback that starts the task handler
@pragma('vm:entry-point')
void startCallback() {
  FlutterForegroundTask.setTaskHandler(TrackerTaskHandler());
}

/// The task handler that runs in the foreground service
class TrackerTaskHandler extends TaskHandler {
  // Configuration
  String _serverHost = TrackerConfig.defaultServerHost;
  int _serverPort = TrackerConfig.defaultServerPort;
  String _sailorId = 'Sailor';
  String _role = 'sailor';
  String _password = '';
  int _eventId = 1;  // Event ID for multi-event support
  String _version = 'flutter';
  bool _highFrequencyMode = false;

  // State
  bool _assistRequested = false;
  int _sequenceNumber = 0;
  int _packetsAcked = 0;
  int _packetsSent = 0;
  DateTime? _lastAckTime;
  final Set<int> _ackedSequences = {};

  // DNS caching
  InternetAddress? _cachedServerAddress;
  DateTime? _lastDnsLookupTime;

  // UDP socket
  RawDatagramSocket? _socket;
  StreamSubscription? _socketSubscription;

  // Location
  StreamSubscription<Position>? _locationSubscription;
  final Battery _battery = Battery();

  // Battery drain tracking
  DateTime? _trackingStartTime;
  int? _trackingStartBattery;

  // 1Hz mode position buffer: [[ts, lat, lon], ...]
  final List<List<dynamic>> _positionBuffer = [];
  Position? _lastBufferedPosition;  // Keep last position for metadata

  // Send throttling for normal mode (10 seconds)
  DateTime? _lastSendTime;
  static const Duration _normalSendInterval = Duration(seconds: 10);

  @override
  Future<void> onStart(DateTime timestamp, TaskStarter starter) async {
    debugPrint('TrackerTaskHandler started');

    // Record starting battery for drain rate calculation
    _trackingStartTime = DateTime.now();
    try {
      _trackingStartBattery = await _battery.batteryLevel;
      debugPrint('Starting battery: $_trackingStartBattery%');
    } catch (e) {
      debugPrint('Failed to get starting battery: $e');
    }

    // Initialize UDP socket
    try {
      _socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
      // Explicitly enable read events (should be default, but be explicit)
      _socket!.readEventsEnabled = true;
      _socketSubscription = _socket!.listen(_handleSocketData, onError: (e) {
        debugPrint('Socket stream error: $e');
      }, onDone: () {
        debugPrint('Socket stream closed');
      });
      debugPrint('Socket bound to port ${_socket!.port}');
    } catch (e) {
      debugPrint('Failed to create socket: $e');
    }

    // Start location stream with platform-specific settings
    try {
      late LocationSettings locationSettings;

      if (Platform.isIOS) {
        // iOS-specific settings for reliable background tracking
        locationSettings = AppleSettings(
          accuracy: LocationAccuracy.high,
          distanceFilter: 0,
          activityType: ActivityType.fitness, // Better for outdoor activities
          pauseLocationUpdatesAutomatically: false, // Critical: don't let iOS pause updates
          allowBackgroundLocationUpdates: true, // Allow updates when backgrounded
          showBackgroundLocationIndicator: true, // Show blue bar (required by Apple)
        );
      } else {
        // Android settings
        locationSettings = const LocationSettings(
          accuracy: LocationAccuracy.high,
          distanceFilter: 0,
        );
      }

      _locationSubscription = Geolocator.getPositionStream(
        locationSettings: locationSettings,
      ).listen(_handleLocationUpdate, onError: (e) {
        debugPrint('Location stream error: $e');
        FlutterForegroundTask.sendDataToMain({
          'type': 'error',
          'message': 'Location stream error: $e',
        });
      });
      debugPrint('Location stream subscription started');

      // Get immediate position
      _getImmediatePosition();
    } catch (e) {
      debugPrint('Failed to start location updates: $e');
      FlutterForegroundTask.sendDataToMain({
        'type': 'error',
        'message': 'Failed to start location: $e',
      });
    }
  }

  @override
  void onRepeatEvent(DateTime timestamp) {
    // Called every 10 seconds by the foreground task

    // Manually poll for any pending ACKs (backup for stream listener)
    _pollForAcks();

    _getImmediatePosition();
  }

  /// Manually poll the socket for pending datagrams.
  /// This is a backup in case the stream listener doesn't work reliably in background.
  void _pollForAcks() {
    final socket = _socket;
    if (socket == null) return;

    // Try to receive any pending datagrams
    Datagram? datagram;
    int count = 0;
    while ((datagram = socket.receive()) != null && count < 10) {
      count++;
      try {
        final response = utf8.decode(datagram!.data);
        final ack = jsonDecode(response) as Map<String, dynamic>;
        final ackSeq = ack['ack'] as int?;

        if (ackSeq != null && ackSeq > 0) {
          // Check for auth error
          final error = ack['error'] as String?;
          if (error == 'auth') {
            final msg = ack['msg'] as String? ?? 'Invalid password';
            debugPrint('Auth error received: $msg');
            FlutterForegroundTask.sendDataToMain({
              'type': 'authError',
              'message': msg,
            });
            // Don't count as successful ACK
            continue;
          }

          _lastAckTime = DateTime.now();
          _packetsAcked++;
          _ackedSequences.add(ackSeq);

          // Keep set from growing indefinitely
          if (_ackedSequences.length > 100) {
            final oldSeqs = _ackedSequences.where((s) => s < ackSeq - 50).toList();
            _ackedSequences.removeAll(oldSeqs);
          }

          final ackRate = _packetsSent > 0 ? _packetsAcked / _packetsSent : 0.0;

          // Extract event name if present
          final eventName = ack['event'] as String?;

          FlutterForegroundTask.sendDataToMain({
            'type': 'ack',
            'seq': ackSeq,
            'packetsAcked': _packetsAcked,
            'ackRate': ackRate,
            if (eventName != null && eventName.isNotEmpty) 'eventName': eventName,
          });

          debugPrint('Polled ACK for seq=$ackSeq${eventName != null ? " (event: $eventName)" : ""}');
        }
      } catch (e) {
        debugPrint('Failed to parse polled ACK: $e');
      }
    }
    if (count > 0) {
      debugPrint('Polled $count ACKs');
    }
  }

  @override
  Future<void> onDestroy(DateTime timestamp) async {
    debugPrint('TrackerTaskHandler destroyed');

    _locationSubscription?.cancel();
    _locationSubscription = null;

    _socketSubscription?.cancel();
    _socketSubscription = null;

    _socket?.close();
    _socket = null;
  }

  @override
  void onReceiveData(Object data) {
    // Receive data from the main app
    if (data is Map<String, dynamic>) {
      final type = data['type'] as String?;

      if (type == 'config') {
        _serverHost = data['serverHost'] as String? ?? _serverHost;
        _serverPort = data['serverPort'] as int? ?? _serverPort;
        _sailorId = data['sailorId'] as String? ?? _sailorId;
        _role = data['role'] as String? ?? _role;
        _password = data['password'] as String? ?? _password;
        _eventId = data['eventId'] as int? ?? _eventId;
        _version = data['version'] as String? ?? _version;
        _highFrequencyMode = data['highFrequencyMode'] as bool? ?? _highFrequencyMode;

        // Clear cached DNS when server changes
        _cachedServerAddress = null;
        _lastDnsLookupTime = null;

        // Clear position buffer and throttle state when mode changes
        _positionBuffer.clear();
        _lastSendTime = null;

        debugPrint('Config updated: $_sailorId -> $_serverHost:$_serverPort (1Hz=${_highFrequencyMode})');

        // Update notification
        FlutterForegroundTask.updateService(
          notificationTitle: 'Windsurfer Tracker',
          notificationText: 'Tracking active - $_sailorId',
        );
      } else if (type == 'assist') {
        _assistRequested = data['enabled'] as bool? ?? false;
        debugPrint('Assist ${_assistRequested ? "ENABLED" : "disabled"}');

        // Send immediate position if assist requested
        if (_assistRequested) {
          _getImmediatePosition();
        }
      }
    }
  }

  Future<void> _getImmediatePosition() async {
    try {
      debugPrint('Getting immediate position...');
      final position = await Geolocator.getCurrentPosition(
        desiredAccuracy: LocationAccuracy.high,
        timeLimit: const Duration(seconds: 5),
      );
      debugPrint('Got position: ${position.latitude}, ${position.longitude} (accuracy: ${position.accuracy}m)');
      _handleLocationUpdate(position);
    } catch (e) {
      debugPrint('Failed to get immediate position: $e');
      FlutterForegroundTask.sendDataToMain({
        'type': 'error',
        'message': 'Position timeout: $e',
      });
    }
  }

  void _handleLocationUpdate(Position position) {
    // Filter out inaccurate locations (technique from OwnTracks)
    if (TrackerConfig.maxAccuracyMeters > 0 &&
        position.accuracy > TrackerConfig.maxAccuracyMeters) {
      debugPrint('Skipping inaccurate location: accuracy=${position.accuracy}m > ${TrackerConfig.maxAccuracyMeters}m');
      return;
    }

    final speedKnots = position.speed * 1.94384; // m/s to knots
    final heading = position.heading.toInt();

    // Send to main app for UI update
    FlutterForegroundTask.sendDataToMain({
      'type': 'location',
      'latitude': position.latitude,
      'longitude': position.longitude,
      'speedKnots': speedKnots,
      'heading': heading < 0 ? 0 : heading,
      'accuracy': position.accuracy,
      'timestamp': position.timestamp?.millisecondsSinceEpoch ?? DateTime.now().millisecondsSinceEpoch,
    });

    if (_highFrequencyMode) {
      // Buffer position for batched sending
      final ts = position.timestamp?.millisecondsSinceEpoch ?? DateTime.now().millisecondsSinceEpoch;
      _positionBuffer.add([ts ~/ 1000, position.latitude, position.longitude]);
      _lastBufferedPosition = position;

      // Send every 10 positions (10 seconds at 1Hz)
      if (_positionBuffer.length >= 10) {
        _sendPositionArray();
      }
    } else {
      // Throttle to every 10 seconds in normal mode
      final now = DateTime.now();
      if (_lastSendTime == null || now.difference(_lastSendTime!) >= _normalSendInterval) {
        _lastSendTime = now;
        _sendPosition(position);
      }
    }
  }

  Future<InternetAddress?> _getServerAddress() async {
    final now = DateTime.now();
    final cached = _cachedServerAddress;
    final lastLookup = _lastDnsLookupTime;

    if (cached == null ||
        lastLookup == null ||
        now.difference(lastLookup) > TrackerConfig.dnsRefreshInterval) {
      try {
        final results = await InternetAddress.lookup(_serverHost);
        if (results.isNotEmpty) {
          final resolved = results.first;
          _cachedServerAddress = resolved;
          _lastDnsLookupTime = now;

          if (cached == null) {
            debugPrint('DNS resolved $_serverHost to ${resolved.address}');
          } else if (resolved.address != cached.address) {
            debugPrint('DNS updated $_serverHost: ${cached.address} -> ${resolved.address}');
          }
          return resolved;
        }
      } catch (e) {
        if (cached != null) {
          debugPrint('DNS lookup failed for $_serverHost, using cached ${cached.address}');
          return cached;
        } else {
          debugPrint('DNS lookup failed for $_serverHost with no cached address: $e');
          return null;
        }
      }
    }

    return cached;
  }

  Future<void> _sendPosition(Position position) async {
    final seq = ++_sequenceNumber;

    // Get battery level
    int batteryPercent = -1;
    try {
      batteryPercent = await _battery.batteryLevel;
    } catch (e) {
      // Battery info not available
    }

    // Get battery saver mode
    bool isPowerSaveMode = false;
    try {
      isPowerSaveMode = await _battery.isInBatterySaveMode;
    } catch (e) {
      // Battery saver info not available
    }

    // Get charging state
    bool isCharging = false;
    try {
      final batteryState = await _battery.batteryState;
      isCharging = batteryState == BatteryState.charging ||
                   batteryState == BatteryState.full;
    } catch (e) {
      // Battery state not available
    }

    // Calculate battery drain rate (%/hr)
    double? drainRate;
    if (_trackingStartTime != null && _trackingStartBattery != null && batteryPercent >= 0) {
      final elapsed = DateTime.now().difference(_trackingStartTime!);
      if (elapsed.inMinutes >= 5) {
        final drainPercent = _trackingStartBattery! - batteryPercent;
        final hoursElapsed = elapsed.inSeconds / 3600.0;
        if (hoursElapsed > 0) {
          drainRate = drainPercent / hoursElapsed;
        }
      }
    }

    final speedKnots = position.speed * 1.94384;
    var heading = position.heading.toInt();
    if (heading < 0) heading = 0;

    // Build flags object
    final flags = {
      'ps': isPowerSaveMode,
      'bo': true, // Assume battery optimization handled
    };

    // Build packet
    final packet = {
      'id': _sailorId,
      'eid': _eventId,
      'sq': seq,
      'ts': DateTime.now().millisecondsSinceEpoch ~/ 1000,
      'lat': position.latitude,
      'lon': position.longitude,
      'spd': speedKnots,
      'hdg': heading,
      'ast': _assistRequested,
      'bat': batteryPercent,
      'sig': -1,
      'role': _role,
      'flg': flags,
      'ver': _version,
      'os': '${Platform.operatingSystem == 'ios' ? 'iOS' : Platform.operatingSystem} ${Platform.operatingSystemVersion}',
      'chg': isCharging,
    };

    if (drainRate != null) {
      packet['bdr'] = double.parse(drainRate.toStringAsFixed(1));
    }

    if (_password.isNotEmpty) {
      packet['pwd'] = _password;
    }

    final data = utf8.encode(jsonEncode(packet));

    final address = await _getServerAddress();
    if (address == null) {
      debugPrint('Cannot send packet - no server address available');
      return;
    }

    // Send with retries, stopping if ACK received
    for (int attempt = 0; attempt < TrackerConfig.udpRetryCount; attempt++) {
      // Check if already ACKed (from previous attempt)
      if (_ackedSequences.contains(seq)) {
        break;
      }

      try {
        _socket?.send(data, address, _serverPort);
        _packetsSent++;

        // Notify main app
        FlutterForegroundTask.sendDataToMain({
          'type': 'packetSent',
          'seq': seq,
          'packetsSent': _packetsSent,
        });

        debugPrint('Sent packet seq=$seq attempt=${attempt + 1}');

        // Wait for ACK before retrying
        if (attempt < TrackerConfig.udpRetryCount - 1) {
          await Future.delayed(TrackerConfig.udpRetryDelay);
        }
      } catch (e) {
        debugPrint('Failed to send packet: $e');
      }
    }
  }

  /// Send buffered positions as an array (1Hz mode)
  Future<void> _sendPositionArray() async {
    if (_positionBuffer.isEmpty) return;

    final position = _lastBufferedPosition;
    if (position == null) {
      _positionBuffer.clear();
      return;
    }

    final seq = ++_sequenceNumber;

    // Get battery level
    int batteryPercent = -1;
    try {
      batteryPercent = await _battery.batteryLevel;
    } catch (e) {
      // Battery info not available
    }

    // Get battery saver mode
    bool isPowerSaveMode = false;
    try {
      isPowerSaveMode = await _battery.isInBatterySaveMode;
    } catch (e) {
      // Battery saver info not available
    }

    // Get charging state
    bool isCharging = false;
    try {
      final batteryState = await _battery.batteryState;
      isCharging = batteryState == BatteryState.charging ||
                   batteryState == BatteryState.full;
    } catch (e) {
      // Battery state not available
    }

    // Calculate battery drain rate (%/hr)
    double? drainRate;
    if (_trackingStartTime != null && _trackingStartBattery != null && batteryPercent >= 0) {
      final elapsed = DateTime.now().difference(_trackingStartTime!);
      if (elapsed.inMinutes >= 5) {
        final drainPercent = _trackingStartBattery! - batteryPercent;
        final hoursElapsed = elapsed.inSeconds / 3600.0;
        if (hoursElapsed > 0) {
          drainRate = drainPercent / hoursElapsed;
        }
      }
    }

    final speedKnots = position.speed * 1.94384;
    var heading = position.heading.toInt();
    if (heading < 0) heading = 0;

    // Build flags object
    final flags = {
      'ps': isPowerSaveMode,
      'bo': true,
    };

    // Build packet with position array
    final packet = {
      'id': _sailorId,
      'eid': _eventId,
      'sq': seq,
      'ts': DateTime.now().millisecondsSinceEpoch ~/ 1000,
      'pos': List<List<dynamic>>.from(_positionBuffer),  // [[ts, lat, lon], ...]
      'spd': speedKnots,
      'hdg': heading,
      'ast': _assistRequested,
      'bat': batteryPercent,
      'sig': -1,
      'role': _role,
      'flg': flags,
      'ver': _version,
      'os': '${Platform.operatingSystem == 'ios' ? 'iOS' : Platform.operatingSystem} ${Platform.operatingSystemVersion}',
      'chg': isCharging,
    };

    if (drainRate != null) {
      packet['bdr'] = double.parse(drainRate.toStringAsFixed(1));
    }

    if (_password.isNotEmpty) {
      packet['pwd'] = _password;
    }

    // Clear buffer after copying
    _positionBuffer.clear();

    final data = utf8.encode(jsonEncode(packet));

    final address = await _getServerAddress();
    if (address == null) {
      debugPrint('Cannot send packet - no server address available');
      return;
    }

    // Send with retries
    for (int attempt = 0; attempt < TrackerConfig.udpRetryCount; attempt++) {
      if (_ackedSequences.contains(seq)) {
        break;
      }

      try {
        _socket?.send(data, address, _serverPort);
        _packetsSent++;

        FlutterForegroundTask.sendDataToMain({
          'type': 'packetSent',
          'seq': seq,
          'packetsSent': _packetsSent,
        });

        debugPrint('Sent array packet seq=$seq with ${(packet['pos'] as List).length} positions, attempt=${attempt + 1}');

        if (attempt < TrackerConfig.udpRetryCount - 1) {
          await Future.delayed(TrackerConfig.udpRetryDelay);
        }
      } catch (e) {
        debugPrint('Failed to send packet: $e');
      }
    }
  }

  void _handleSocketData(RawSocketEvent event) {
    debugPrint('Socket event: $event');
    if (event != RawSocketEvent.read) return;

    final datagram = _socket?.receive();
    if (datagram == null) return;

    try {
      final response = utf8.decode(datagram.data);
      final ack = jsonDecode(response) as Map<String, dynamic>;
      final ackSeq = ack['ack'] as int?;

      if (ackSeq != null && ackSeq > 0) {
        // Check for auth error
        final error = ack['error'] as String?;
        if (error == 'auth') {
          final msg = ack['msg'] as String? ?? 'Invalid password';
          debugPrint('Auth error received: $msg');
          FlutterForegroundTask.sendDataToMain({
            'type': 'authError',
            'message': msg,
          });
          return;  // Don't count as successful ACK
        }

        _lastAckTime = DateTime.now();
        _packetsAcked++;
        _ackedSequences.add(ackSeq);

        // Keep set from growing indefinitely
        if (_ackedSequences.length > 100) {
          final oldSeqs = _ackedSequences.where((s) => s < ackSeq - 50).toList();
          _ackedSequences.removeAll(oldSeqs);
        }

        final ackRate = _packetsSent > 0 ? _packetsAcked / _packetsSent : 0.0;

        // Extract event name if present
        final eventName = ack['event'] as String?;

        // Notify main app
        FlutterForegroundTask.sendDataToMain({
          'type': 'ack',
          'seq': ackSeq,
          'packetsAcked': _packetsAcked,
          'ackRate': ackRate,
          if (eventName != null && eventName.isNotEmpty) 'eventName': eventName,
        });

        debugPrint('Received ACK for seq=$ackSeq${eventName != null ? " (event: $eventName)" : ""}');
      }
    } catch (e) {
      debugPrint('Failed to parse ACK: $e');
    }
  }
}
