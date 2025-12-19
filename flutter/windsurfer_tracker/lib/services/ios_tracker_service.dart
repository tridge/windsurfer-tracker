import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:geolocator/geolocator.dart';
import 'package:geolocator_apple/geolocator_apple.dart';
import 'package:battery_plus/battery_plus.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:http/http.dart' as http;
import 'foreground_task_handler.dart';

/// iOS-specific tracker that runs in the main app context.
/// On iOS, flutter_foreground_task doesn't run a persistent service,
/// so we need to manage location tracking directly.
class IOSTrackerService {
  static IOSTrackerService? _instance;
  static IOSTrackerService get instance => _instance ??= IOSTrackerService._();

  IOSTrackerService._();

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
  bool _isRunning = false;
  bool _assistRequested = false;
  int _sequenceNumber = 0;
  int _packetsAcked = 0;
  int _packetsSent = 0;
  DateTime? _lastAckTime;
  final Set<int> _ackedSequences = {};

  // DNS caching
  InternetAddress? _cachedServerAddress;
  DateTime? _lastDnsLookupTime;
  bool _isIPv6Socket = false;

  // HTTP fallback state
  bool _useHttpFallback = false;
  DateTime? _lastUdpRetryTime;
  int _consecutiveUdpFailures = 0;
  static const int _udpFailureThreshold = 3;  // Switch to HTTP after this many failures
  static const Duration _udpRetryInterval = Duration(minutes: 1);  // Retry UDP every minute
  static const Duration _httpTimeout = Duration(seconds: 10);  // HTTP POST timeout
  static const Duration _normalSendInterval = Duration(seconds: 10);  // 10 second interval for normal mode

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
  Position? _lastBufferedPosition;

  // Send throttling for normal mode
  DateTime? _lastSendTime;

  // Watchdog timer for when location stream stops (e.g., low power mode)
  Timer? _watchdogTimer;
  DateTime? _lastLocationTime;
  static const Duration _watchdogInterval = Duration(seconds: 15);

  // Callback for UI updates
  void Function(Map<String, dynamic>)? onDataReceived;

  bool get isRunning => _isRunning;

  Future<void> configure({
    required String serverHost,
    required int serverPort,
    required String sailorId,
    required String role,
    required String password,
    int eventId = 1,
    bool highFrequencyMode = false,
  }) async {
    _serverHost = serverHost;
    _serverPort = serverPort;
    _sailorId = sailorId;
    _role = role;
    _password = password;
    _eventId = eventId;
    _highFrequencyMode = highFrequencyMode;

    // Clear cached DNS when config changes
    _cachedServerAddress = null;
    _lastDnsLookupTime = null;

    // Clear position buffer and throttle state when mode changes
    _positionBuffer.clear();
    _lastSendTime = null;

    // Get version string with git hash if available
    try {
      final packageInfo = await PackageInfo.fromPlatform();
      const gitHash = String.fromEnvironment('GIT_HASH', defaultValue: '');
      if (gitHash.isNotEmpty) {
        _version = '${packageInfo.version}+${packageInfo.buildNumber}($gitHash)';
      } else {
        _version = '${packageInfo.version}+${packageInfo.buildNumber}(flutter)';
      }
    } catch (e) {
      debugPrint('Failed to get package info: $e');
    }

    debugPrint('iOS Tracker configured: $_sailorId -> $_serverHost:$_serverPort (pwd=${_password.isNotEmpty ? "set" : "empty"})');
  }

  Future<void> start() async {
    if (_isRunning) return;
    _isRunning = true;

    debugPrint('Starting iOS Tracker Service');

    // Record starting battery for drain rate calculation
    _trackingStartTime = DateTime.now();
    try {
      _trackingStartBattery = await _battery.batteryLevel;
      debugPrint('Starting battery: $_trackingStartBattery%');
    } catch (e) {
      debugPrint('Failed to get starting battery: $e');
    }

    // First resolve DNS to determine what address type is available
    InternetAddressType addressType = InternetAddressType.IPv4;
    try {
      final addresses = await InternetAddress.lookup(_serverHost);
      if (addresses.isNotEmpty) {
        // Check if we have IPv6 addresses (indicates NAT64/DNS64 or native IPv6)
        final hasIPv6 = addresses.any((a) => a.type == InternetAddressType.IPv6);
        final hasIPv4 = addresses.any((a) => a.type == InternetAddressType.IPv4);

        if (hasIPv6 && !hasIPv4) {
          // IPv6 only (likely NAT64 network) - must use IPv6
          addressType = InternetAddressType.IPv6;
          debugPrint('DNS returned IPv6 only - using IPv6 socket');
        } else if (hasIPv6 && hasIPv4) {
          // Dual-stack - prefer IPv4 for compatibility
          addressType = InternetAddressType.IPv4;
          debugPrint('DNS returned both IPv4 and IPv6 - using IPv4 socket');
        } else {
          // IPv4 only
          addressType = InternetAddressType.IPv4;
          debugPrint('DNS returned IPv4 only - using IPv4 socket');
        }
      }
    } catch (e) {
      debugPrint('DNS lookup failed during socket setup: $e - defaulting to IPv4');
    }

    // Create socket matching the DNS result
    try {
      if (addressType == InternetAddressType.IPv6) {
        _socket = await RawDatagramSocket.bind(InternetAddress.anyIPv6, 0);
        _isIPv6Socket = true;
      } else {
        _socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
        _isIPv6Socket = false;
      }
      _socket!.readEventsEnabled = true;
      _socketSubscription = _socket!.listen(_handleSocketData, onError: (e) {
        debugPrint('Socket stream error: $e');
      });
      debugPrint('Created ${_isIPv6Socket ? "IPv6" : "IPv4"} UDP socket on port ${_socket!.port}');
    } catch (e) {
      debugPrint('Failed to create ${addressType.name} socket: $e');
      // Try the other type as fallback
      try {
        if (addressType == InternetAddressType.IPv6) {
          _socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
          _isIPv6Socket = false;
        } else {
          _socket = await RawDatagramSocket.bind(InternetAddress.anyIPv6, 0);
          _isIPv6Socket = true;
        }
        _socket!.readEventsEnabled = true;
        _socketSubscription = _socket!.listen(_handleSocketData, onError: (e2) {
          debugPrint('Socket stream error: $e2');
        });
        debugPrint('Fallback: Created ${_isIPv6Socket ? "IPv6" : "IPv4"} UDP socket on port ${_socket!.port}');
      } catch (e2) {
        debugPrint('Failed to create any socket: $e2');
        _sendToUI({'type': 'error', 'message': 'Failed to create socket: $e2'});
      }
    }

    // Clear cached DNS so next lookup uses correct preference
    _cachedServerAddress = null;
    _lastDnsLookupTime = null;

    // Start location stream with iOS-specific settings
    try {
      final locationSettings = AppleSettings(
        accuracy: LocationAccuracy.high,
        distanceFilter: 0,
        activityType: ActivityType.fitness,
        pauseLocationUpdatesAutomatically: false,
        allowBackgroundLocationUpdates: true,
        showBackgroundLocationIndicator: true,
      );

      _locationSubscription = Geolocator.getPositionStream(
        locationSettings: locationSettings,
      ).listen(_handleLocationUpdate, onError: (e) {
        debugPrint('Location stream error: $e');
        _sendToUI({'type': 'error', 'message': 'Location stream error: $e'});
      });
      debugPrint('Location stream subscription started');

      // Get immediate position
      _getImmediatePosition();

      // Start watchdog timer to poll location if stream stops (e.g., low power mode)
      _startWatchdogTimer();
    } catch (e) {
      debugPrint('Failed to start location updates: $e');
      _sendToUI({'type': 'error', 'message': 'Failed to start location: $e'});
    }
  }

  Future<void> stop() async {
    if (!_isRunning) return;
    _isRunning = false;

    debugPrint('Stopping iOS Tracker Service');

    await _locationSubscription?.cancel();
    _locationSubscription = null;

    _stopWatchdogTimer();

    await _socketSubscription?.cancel();
    _socketSubscription = null;

    _socket?.close();
    _socket = null;

    _assistRequested = false;
  }

  void setAssistRequested(bool value) {
    _assistRequested = value;
  }

  void _sendToUI(Map<String, dynamic> data) {
    onDataReceived?.call(data);
  }

  void _handleSocketData(RawSocketEvent event) {
    if (event != RawSocketEvent.read) return;

    final socket = _socket;
    if (socket == null) return;

    final datagram = socket.receive();
    if (datagram == null) return;

    try {
      final response = utf8.decode(datagram.data);
      final ack = jsonDecode(response) as Map<String, dynamic>;
      final ackSeq = ack['ack'] as int?;

      if (ackSeq != null && ackSeq > 0) {
        _lastAckTime = DateTime.now();
        _packetsAcked++;
        _ackedSequences.add(ackSeq);

        // Keep set from growing indefinitely - remove old sequences
        if (_ackedSequences.length > 100) {
          final oldSeqs = _ackedSequences.where((s) => s < ackSeq - 50).toList();
          _ackedSequences.removeAll(oldSeqs);
        }

        final ackRate = _packetsSent > 0 ? (_packetsAcked / _packetsSent) * 100 : 0.0;

        _sendToUI({
          'type': 'ack',
          'seq': ackSeq,
          'ackRate': ackRate,
        });
      }
    } catch (e) {
      debugPrint('Error parsing ACK: $e');
    }
  }

  Future<void> _getImmediatePosition() async {
    try {
      debugPrint('Getting immediate position...');
      final position = await Geolocator.getCurrentPosition(
        desiredAccuracy: LocationAccuracy.high,
        timeLimit: const Duration(seconds: 10),
      );
      debugPrint('Got position: ${position.latitude}, ${position.longitude} (accuracy: ${position.accuracy}m)');
      _handleLocationUpdate(position);
    } catch (e) {
      debugPrint('Failed to get immediate position: $e');
      _sendToUI({'type': 'error', 'message': 'Position timeout: $e'});
    }
  }

  void _startWatchdogTimer() {
    _watchdogTimer?.cancel();
    _watchdogTimer = Timer.periodic(_watchdogInterval, (_) {
      if (!_isRunning) return;

      // Check if location stream has been silent
      final now = DateTime.now();
      if (_lastLocationTime == null ||
          now.difference(_lastLocationTime!) > _watchdogInterval) {
        debugPrint('Watchdog: Location stream silent, polling manually');
        _getImmediatePosition();
      }
    });
    debugPrint('Watchdog timer started (${_watchdogInterval.inSeconds}s interval)');
  }

  void _stopWatchdogTimer() {
    _watchdogTimer?.cancel();
    _watchdogTimer = null;
  }

  void _handleLocationUpdate(Position position) {
    // Filter out inaccurate locations
    if (TrackerConfig.maxAccuracyMeters > 0 &&
        position.accuracy > TrackerConfig.maxAccuracyMeters) {
      debugPrint('Skipping inaccurate location: accuracy=${position.accuracy}m');
      return;
    }

    // Track when we last received a valid location (for watchdog timer)
    _lastLocationTime = DateTime.now();

    final speedKnots = position.speed * 1.94384;
    final heading = position.heading.toInt();

    // Send to UI
    _sendToUI({
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
        _sendPositionArrayToServer();
      }
    } else {
      // Throttle to every 10 seconds in normal mode
      final now = DateTime.now();
      if (_lastSendTime == null || now.difference(_lastSendTime!) >= _normalSendInterval) {
        _lastSendTime = now;
        _sendPositionToServer(position);
      }
    }
  }

  Future<void> _sendPositionToServer(Position position) async {
    // Get battery level and power save mode
    int batteryLevel = -1;
    bool isPowerSaveMode = false;
    try {
      batteryLevel = await _battery.batteryLevel;
    } catch (e) {
      debugPrint('Failed to get battery level: $e');
    }
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
      debugPrint('Failed to get battery state: $e');
    }

    // Calculate battery drain rate (%/hr)
    double? drainRate;
    if (_trackingStartTime != null && _trackingStartBattery != null && batteryLevel >= 0) {
      final elapsed = DateTime.now().difference(_trackingStartTime!);
      if (elapsed.inMinutes >= 5) {
        final drainPercent = _trackingStartBattery! - batteryLevel;
        final hoursElapsed = elapsed.inSeconds / 3600.0;
        if (hoursElapsed > 0) {
          drainRate = drainPercent / hoursElapsed;
        }
      }
    }

    _sequenceNumber++;
    final speedKnots = position.speed * 1.94384;

    final packet = {
      'id': _sailorId,
      'eid': _eventId,
      'sq': _sequenceNumber,
      'ts': DateTime.now().millisecondsSinceEpoch ~/ 1000,
      'lat': position.latitude,
      'lon': position.longitude,
      'spd': speedKnots,
      'hdg': position.heading.toInt().clamp(0, 360),
      'ast': _assistRequested,
      'bat': batteryLevel,
      'sig': -1,
      'role': _role,
      'ver': _version,
      'os': '${Platform.operatingSystem == 'ios' ? 'iOS' : Platform.operatingSystem} ${Platform.operatingSystemVersion}',
      'ps': isPowerSaveMode,
      'chg': isCharging,
    };

    // Add password if set
    if (_password.isNotEmpty) {
      packet['pwd'] = _password;
    }

    // Add battery drain rate if available
    if (drainRate != null) {
      packet['bdr'] = double.parse(drainRate.toStringAsFixed(1));
    }

    final seqToSend = _sequenceNumber;

    // Check if we should retry UDP (periodically when in fallback mode)
    if (_useHttpFallback) {
      final now = DateTime.now();
      if (_lastUdpRetryTime == null ||
          now.difference(_lastUdpRetryTime!) >= _udpRetryInterval) {
        _lastUdpRetryTime = now;
        debugPrint('Retrying UDP after HTTP fallback period');
        final udpSuccess = await _sendViaUdp(packet, seqToSend);
        if (udpSuccess) {
          debugPrint('UDP retry succeeded, switching back to UDP');
          _useHttpFallback = false;
          _consecutiveUdpFailures = 0;
          return;
        }
      }
      // Use HTTP fallback
      await _sendViaHttp(packet, seqToSend);
    } else {
      // Try UDP first
      final udpSuccess = await _sendViaUdp(packet, seqToSend);
      if (!udpSuccess) {
        _consecutiveUdpFailures++;
        if (_consecutiveUdpFailures >= _udpFailureThreshold) {
          debugPrint('UDP failed $_consecutiveUdpFailures times, switching to HTTP fallback');
          _useHttpFallback = true;
          _lastUdpRetryTime = DateTime.now();
          // Try HTTP immediately
          await _sendViaHttp(packet, seqToSend);
        }
      } else {
        _consecutiveUdpFailures = 0;
      }
    }
  }

  /// Send packet via UDP. Returns true if ACK received.
  Future<bool> _sendViaUdp(Map<String, dynamic> packet, int seqToSend) async {
    final socket = _socket;
    if (socket == null) return false;

    // Resolve server address
    final serverAddress = await _resolveServerAddress();
    if (serverAddress == null) {
      debugPrint('Failed to resolve server address for UDP');
      return false;
    }

    final data = utf8.encode(jsonEncode(packet));

    // Send with retries, stopping if ACK received
    for (int attempt = 0; attempt < TrackerConfig.udpRetryCount; attempt++) {
      if (_ackedSequences.contains(seqToSend)) {
        return true;  // ACK received
      }

      try {
        socket.send(data, serverAddress, _serverPort);
        _packetsSent++;
        _sendToUI({'type': 'packetSent', 'seq': seqToSend, 'method': 'UDP'});

        // Wait for ACK before retrying
        if (attempt < TrackerConfig.udpRetryCount - 1) {
          await Future.delayed(TrackerConfig.udpRetryDelay);
        }
      } catch (e) {
        debugPrint('UDP send failed (attempt ${attempt + 1}): $e');
      }
    }

    // Check if ACK received after all retries
    return _ackedSequences.contains(seqToSend);
  }

  /// Send packet via HTTP POST. Returns true on success.
  Future<bool> _sendViaHttp(Map<String, dynamic> packet, int seqToSend) async {
    final url = Uri.parse('http://$_serverHost:$_serverPort/api/tracker');
    final body = jsonEncode(packet);

    try {
      debugPrint('Sending via HTTP POST to $url');
      final response = await http.post(
        url,
        headers: {'Content-Type': 'application/json'},
        body: body,
      ).timeout(_httpTimeout);

      if (response.statusCode == 200) {
        final ack = jsonDecode(response.body);
        final ackSeq = ack['ack'] as int?;
        if (ackSeq != null && ackSeq > 0) {
          _lastAckTime = DateTime.now();
          _packetsAcked++;
          _ackedSequences.add(ackSeq);
          _sendToUI({'type': 'ack', 'seq': ackSeq, 'method': 'HTTP'});
          debugPrint('HTTP POST success, ACK seq=$ackSeq');
          return true;
        }
      } else {
        debugPrint('HTTP POST failed: ${response.statusCode} ${response.body}');
      }
    } catch (e) {
      debugPrint('HTTP POST error: $e');
    }

    _sendToUI({'type': 'packetSent', 'seq': seqToSend, 'method': 'HTTP', 'failed': true});
    return false;
  }

  /// Send buffered positions as an array (1Hz mode)
  Future<void> _sendPositionArrayToServer() async {
    if (_positionBuffer.isEmpty) return;

    final position = _lastBufferedPosition;
    if (position == null) {
      _positionBuffer.clear();
      return;
    }

    // Get battery level and power save mode
    int batteryLevel = -1;
    bool isPowerSaveMode = false;
    try {
      batteryLevel = await _battery.batteryLevel;
    } catch (e) {
      debugPrint('Failed to get battery level: $e');
    }
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
      debugPrint('Failed to get battery state: $e');
    }

    // Calculate battery drain rate (%/hr)
    double? drainRate;
    if (_trackingStartTime != null && _trackingStartBattery != null && batteryLevel >= 0) {
      final elapsed = DateTime.now().difference(_trackingStartTime!);
      if (elapsed.inMinutes >= 5) {
        final drainPercent = _trackingStartBattery! - batteryLevel;
        final hoursElapsed = elapsed.inSeconds / 3600.0;
        if (hoursElapsed > 0) {
          drainRate = drainPercent / hoursElapsed;
        }
      }
    }

    _sequenceNumber++;
    final speedKnots = position.speed * 1.94384;

    final packet = {
      'id': _sailorId,
      'eid': _eventId,
      'sq': _sequenceNumber,
      'ts': DateTime.now().millisecondsSinceEpoch ~/ 1000,
      'pos': List<List<dynamic>>.from(_positionBuffer),  // [[ts, lat, lon], ...]
      'spd': speedKnots,
      'hdg': position.heading.toInt().clamp(0, 360),
      'ast': _assistRequested,
      'bat': batteryLevel,
      'sig': -1,
      'role': _role,
      'ver': _version,
      'os': '${Platform.operatingSystem == 'ios' ? 'iOS' : Platform.operatingSystem} ${Platform.operatingSystemVersion}',
      'ps': isPowerSaveMode,
      'chg': isCharging,
    };

    // Add password if set
    if (_password.isNotEmpty) {
      packet['pwd'] = _password;
    }

    // Add battery drain rate if available
    if (drainRate != null) {
      packet['bdr'] = double.parse(drainRate.toStringAsFixed(1));
    }

    // Clear buffer after copying
    _positionBuffer.clear();

    final seqToSend = _sequenceNumber;
    final posCount = (packet['pos'] as List).length;

    // Use same UDP/HTTP fallback logic as single position
    if (_useHttpFallback) {
      final now = DateTime.now();
      if (_lastUdpRetryTime == null ||
          now.difference(_lastUdpRetryTime!) >= _udpRetryInterval) {
        _lastUdpRetryTime = now;
        debugPrint('Retrying UDP for array packet ($posCount positions)');
        final udpSuccess = await _sendViaUdp(packet, seqToSend);
        if (udpSuccess) {
          debugPrint('UDP retry succeeded for array packet');
          _useHttpFallback = false;
          _consecutiveUdpFailures = 0;
          return;
        }
      }
      await _sendViaHttp(packet, seqToSend);
    } else {
      final udpSuccess = await _sendViaUdp(packet, seqToSend);
      if (!udpSuccess) {
        _consecutiveUdpFailures++;
        if (_consecutiveUdpFailures >= _udpFailureThreshold) {
          debugPrint('UDP failed for array packets, switching to HTTP');
          _useHttpFallback = true;
          _lastUdpRetryTime = DateTime.now();
          await _sendViaHttp(packet, seqToSend);
        }
      } else {
        _consecutiveUdpFailures = 0;
        debugPrint('Sent array packet seq=$seqToSend with $posCount positions via UDP');
      }
    }
  }

  Future<InternetAddress?> _resolveServerAddress() async {
    final now = DateTime.now();

    if (_cachedServerAddress != null &&
        _lastDnsLookupTime != null &&
        now.difference(_lastDnsLookupTime!) < TrackerConfig.dnsRefreshInterval) {
      return _cachedServerAddress;
    }

    try {
      final addresses = await InternetAddress.lookup(_serverHost);
      if (addresses.isNotEmpty) {
        // Prefer address type matching our socket
        InternetAddress resolved;
        if (_isIPv6Socket) {
          resolved = addresses.firstWhere(
            (a) => a.type == InternetAddressType.IPv6,
            orElse: () => addresses.first,
          );
        } else {
          resolved = addresses.firstWhere(
            (a) => a.type == InternetAddressType.IPv4,
            orElse: () => addresses.first,
          );
        }
        _cachedServerAddress = resolved;
        _lastDnsLookupTime = now;
        debugPrint('Resolved $_serverHost to ${resolved.address} (${resolved.type.name})');
        return _cachedServerAddress;
      }
    } catch (e) {
      debugPrint('DNS lookup failed for $_serverHost: $e');
    }

    return _cachedServerAddress;
  }
}
