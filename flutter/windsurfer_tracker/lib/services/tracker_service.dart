import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:geolocator/geolocator.dart';
import 'package:battery_plus/battery_plus.dart';
import 'package:package_info_plus/package_info_plus.dart';

/// Configuration constants matching Android app
class TrackerConfig {
  static const String defaultServerHost = 'wstracker.org';
  static const int defaultServerPort = 41234;
  static const Duration locationInterval = Duration(seconds: 10);
  static const int udpRetryCount = 3;
  static const Duration udpRetryDelay = Duration(milliseconds: 1500);
  static const Duration ackTimeout = Duration(seconds: 2);
  static const Duration dnsRefreshInterval = Duration(minutes: 5);
}

/// Position data from GPS
class TrackerPosition {
  final double latitude;
  final double longitude;
  final double speedKnots;
  final int heading;
  final DateTime timestamp;

  TrackerPosition({
    required this.latitude,
    required this.longitude,
    required this.speedKnots,
    required this.heading,
    required this.timestamp,
  });
}

/// Callback interface for UI updates
typedef OnLocationUpdate = void Function(TrackerPosition position);
typedef OnAckReceived = void Function(int seq);
typedef OnPacketSent = void Function(int seq);
typedef OnConnectionStatus = void Function(double ackRate);
typedef OnAuthError = void Function(String message);

/// Main tracker service - handles UDP communication and location tracking
class TrackerService {
  // Configuration
  String serverHost;
  int serverPort;
  String sailorId;
  String role;
  String password;
  int eventId;  // Event ID for multi-event support
  String version;

  /// Get version string in format "1.0.0+1(flutter)"
  static Future<String> getVersionString() async {
    try {
      final packageInfo = await PackageInfo.fromPlatform();
      return '${packageInfo.version}+${packageInfo.buildNumber}(flutter)';
    } catch (e) {
      return 'flutter';
    }
  }

  // Callbacks
  OnLocationUpdate? onLocationUpdate;
  OnAckReceived? onAckReceived;
  OnPacketSent? onPacketSent;
  OnConnectionStatus? onConnectionStatus;
  OnAuthError? onAuthError;

  // State
  bool _isRunning = false;
  bool _assistRequested = false;
  int _sequenceNumber = 0;
  int _packetsAcked = 0;
  int _packetsSent = 0;
  DateTime? _lastAckTime;
  TrackerPosition? _lastPosition;

  // Track acknowledged sequence numbers to stop retransmissions
  final Set<int> _acknowledgedSeqs = {};

  // DNS caching
  InternetAddress? _cachedServerAddress;
  DateTime? _lastDnsLookupTime;

  // UDP socket
  RawDatagramSocket? _socket;

  // Location
  StreamSubscription<Position>? _locationSubscription;
  final Battery _battery = Battery();

  TrackerService({
    this.serverHost = TrackerConfig.defaultServerHost,
    this.serverPort = TrackerConfig.defaultServerPort,
    this.sailorId = 'Sailor',
    this.role = 'sailor',
    this.password = '',
    this.eventId = 1,
    this.version = 'flutter',
  });

  bool get isRunning => _isRunning;
  bool get isAssistActive => _assistRequested;
  TrackerPosition? get lastPosition => _lastPosition;
  DateTime? get lastAckTime => _lastAckTime;

  double get ackRate {
    if (_packetsSent == 0) return 0.0;
    return _packetsAcked / _packetsSent;
  }

  // Track socket type for address selection
  bool _isIPv6Socket = false;

  /// Get server address with DNS caching
  Future<InternetAddress?> _getServerAddress() async {
    final now = DateTime.now();
    final cached = _cachedServerAddress;
    final lastLookup = _lastDnsLookupTime;

    // If no cached address or time to refresh, do DNS lookup
    if (cached == null ||
        lastLookup == null ||
        now.difference(lastLookup) > TrackerConfig.dnsRefreshInterval) {
      try {
        final results = await InternetAddress.lookup(serverHost);
        if (results.isNotEmpty) {
          // Prefer address type matching our socket
          InternetAddress? resolved;
          if (_isIPv6Socket) {
            // Prefer IPv6 for IPv6 socket
            resolved = results.firstWhere(
              (a) => a.type == InternetAddressType.IPv6,
              orElse: () => results.first,
            );
          } else {
            // Prefer IPv4 for IPv4 socket
            resolved = results.firstWhere(
              (a) => a.type == InternetAddressType.IPv4,
              orElse: () => results.first,
            );
          }

          _cachedServerAddress = resolved;
          _lastDnsLookupTime = now;

          if (cached == null) {
            debugPrint('DNS resolved $serverHost to ${resolved.address} (${resolved.type.name})');
          } else if (resolved.address != cached.address) {
            debugPrint(
                'DNS updated $serverHost: ${cached.address} -> ${resolved.address}');
          }
          return resolved;
        }
      } catch (e) {
        if (cached != null) {
          debugPrint(
              'DNS lookup failed for $serverHost, using cached ${cached.address}');
          return cached;
        } else {
          debugPrint('DNS lookup failed for $serverHost with no cached address: $e');
          return null;
        }
      }
    }

    return cached;
  }

  /// Start tracking
  Future<bool> start() async {
    if (_isRunning) {
      debugPrint('Already tracking');
      return true;
    }

    debugPrint('Starting tracking to $serverHost:$serverPort as $sailorId');

    // First resolve DNS to determine what address type is available
    InternetAddressType addressType = InternetAddressType.IPv4;
    try {
      final addresses = await InternetAddress.lookup(serverHost);
      if (addresses.isNotEmpty) {
        final hasIPv6 = addresses.any((a) => a.type == InternetAddressType.IPv6);
        final hasIPv4 = addresses.any((a) => a.type == InternetAddressType.IPv4);

        if (hasIPv6 && !hasIPv4) {
          // IPv6 only (likely NAT64 network) - must use IPv6
          addressType = InternetAddressType.IPv6;
          debugPrint('DNS returned IPv6 only - using IPv6 socket');
        } else {
          // IPv4 available - prefer IPv4 for compatibility
          addressType = InternetAddressType.IPv4;
          debugPrint('DNS returned IPv4 - using IPv4 socket');
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
      _socket!.listen(_handleSocketData);
      debugPrint('Created ${_isIPv6Socket ? "IPv6" : "IPv4"} UDP socket');
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
        _socket!.listen(_handleSocketData);
        debugPrint('Fallback: Created ${_isIPv6Socket ? "IPv6" : "IPv4"} UDP socket');
      } catch (e2) {
        debugPrint('Failed to create any socket: $e2');
        return false;
      }
    }

    // Clear cached DNS so next lookup uses correct preference
    _cachedServerAddress = null;

    // Start location updates
    try {
      const locationSettings = LocationSettings(
        accuracy: LocationAccuracy.high,
        distanceFilter: 0,
      );

      _locationSubscription = Geolocator.getPositionStream(
        locationSettings: locationSettings,
      ).listen(_handleLocationUpdate);

      // Also get immediate position
      _getImmediatePosition();
    } catch (e) {
      debugPrint('Failed to start location updates: $e');
      _socket?.close();
      _socket = null;
      return false;
    }

    _isRunning = true;
    _sequenceNumber = 0;
    _packetsAcked = 0;
    _packetsSent = 0;
    _acknowledgedSeqs.clear();

    // Start periodic location timer (in case stream doesn't fire often enough)
    _startPeriodicLocationCheck();

    return true;
  }

  /// Stop tracking
  void stop() {
    if (!_isRunning) return;

    debugPrint('Stopping tracking');
    _isRunning = false;

    _locationSubscription?.cancel();
    _locationSubscription = null;

    _socket?.close();
    _socket = null;

    _periodicTimer?.cancel();
    _periodicTimer = null;
  }

  Timer? _periodicTimer;

  void _startPeriodicLocationCheck() {
    _periodicTimer?.cancel();
    _periodicTimer = Timer.periodic(TrackerConfig.locationInterval, (_) {
      if (_isRunning) {
        _getImmediatePosition();
      }
    });
  }

  Future<void> _getImmediatePosition() async {
    try {
      final position = await Geolocator.getCurrentPosition(
        desiredAccuracy: LocationAccuracy.high,
        timeLimit: const Duration(seconds: 5),
      );
      _handleLocationUpdate(position);
    } catch (e) {
      debugPrint('Failed to get immediate position: $e');
    }
  }

  void _handleLocationUpdate(Position position) {
    final speedKnots = position.speed * 1.94384; // m/s to knots
    final heading = position.heading.toInt();

    final trackerPosition = TrackerPosition(
      latitude: position.latitude,
      longitude: position.longitude,
      speedKnots: speedKnots,
      heading: heading < 0 ? 0 : heading,
      timestamp: position.timestamp ?? DateTime.now(),
    );

    _lastPosition = trackerPosition;
    onLocationUpdate?.call(trackerPosition);

    _sendPosition(trackerPosition);
  }

  Future<void> _sendPosition(TrackerPosition position) async {
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

    // Build flags object for status indicators
    final flags = {
      'ps': isPowerSaveMode,  // Power save mode (system battery saver)
      'bo': true,             // Battery optimization - assume ignored for Flutter (handled by system)
    };

    // Build packet matching Android format
    final packet = {
      'id': sailorId,
      'eid': eventId,
      'sq': seq,
      'ts': DateTime.now().millisecondsSinceEpoch ~/ 1000,
      'lat': position.latitude,
      'lon': position.longitude,
      'spd': position.speedKnots,
      'hdg': position.heading,
      'ast': _assistRequested,
      'bat': batteryPercent,
      'sig': -1, // Signal strength not easily available in Flutter
      'role': role,
      'flg': flags,  // Status flags
      'ver': version,
      'os': '${Platform.operatingSystem == 'ios' ? 'iOS' : Platform.operatingSystem} ${Platform.operatingSystemVersion}',
    };

    if (password.isNotEmpty) {
      packet['pwd'] = password;
    }

    final data = utf8.encode(jsonEncode(packet));

    final address = await _getServerAddress();
    if (address == null) {
      debugPrint('Cannot send packet - no server address available');
      return;
    }

    // Send with retries
    for (int attempt = 0; attempt < TrackerConfig.udpRetryCount; attempt++) {
      // Stop retrying if we already got an ACK for this sequence
      if (_acknowledgedSeqs.contains(seq)) {
        debugPrint('Stopping retries for seq=$seq - already acknowledged');
        return;
      }

      try {
        _socket?.send(data, address, serverPort);
        _packetsSent++;
        onPacketSent?.call(seq);
        debugPrint('Sent packet seq=$seq attempt=${attempt + 1}');

        if (attempt < TrackerConfig.udpRetryCount - 1) {
          await Future.delayed(TrackerConfig.udpRetryDelay);
        }
      } catch (e) {
        debugPrint('Failed to send packet: $e');
      }
    }
  }

  void _handleSocketData(RawSocketEvent event) {
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
          onAuthError?.call(msg);
          // Don't count as successful ACK
          return;
        }

        // Mark this sequence as acknowledged to stop retransmissions
        _acknowledgedSeqs.add(ackSeq);

        // Clean up old sequence numbers (keep only recent ones)
        _acknowledgedSeqs.removeWhere((seq) => seq < _sequenceNumber - 100);

        _lastAckTime = DateTime.now();
        _packetsAcked++;

        onAckReceived?.call(ackSeq);
        onConnectionStatus?.call(ackRate);

        debugPrint('Received ACK for seq=$ackSeq');
      }
    } catch (e) {
      debugPrint('Failed to parse ACK: $e');
    }
  }

  /// Request assistance
  void requestAssist(bool enabled) {
    _assistRequested = enabled;
    debugPrint('Assist ${enabled ? "ENABLED" : "disabled"}');

    // Send immediate position update if requesting assist
    if (enabled && _lastPosition != null) {
      _sendPosition(_lastPosition!);
    }
  }
}
