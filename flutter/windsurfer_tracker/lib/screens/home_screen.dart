import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:flutter_foreground_task/models/service_request_result.dart';
import 'package:geolocator/geolocator.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:vibration/vibration.dart';
import 'package:wakelock_plus/wakelock_plus.dart';
import 'package:intl/intl.dart';
import 'package:package_info_plus/package_info_plus.dart';
import '../services/foreground_task_handler.dart';
import '../services/ios_tracker_service.dart';
import '../services/preferences_service.dart';
import '../widgets/assist_button.dart';
import '../widgets/settings_dialog.dart';

/// Position data for UI display
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

class HomeScreen extends StatefulWidget {
  final PreferencesService prefs;

  const HomeScreen({super.key, required this.prefs});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with WidgetsBindingObserver {
  bool _isTracking = false;
  bool _assistActive = false;

  // Status display
  TrackerPosition? _lastPosition;
  int _lastAckSeq = 0;
  double _ackRate = 0.0;
  String _lastUpdateTime = '--:--:--';
  String _eventName = '';

  @override
  void initState() {
    super.initState();

    // Add lifecycle observer
    WidgetsBinding.instance.addObserver(this);

    // Initialize communication port to receive data from running foreground task
    FlutterForegroundTask.initCommunicationPort();

    // Listen for data from foreground task
    FlutterForegroundTask.addTaskDataCallback(_onReceiveTaskData);

    // Check for auto-resume - check if task is already running
    _checkRunningTask();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      // Re-initialize communication port when app resumes
      FlutterForegroundTask.initCommunicationPort();
      // Re-check running state and re-send config when app resumes
      _checkRunningTask();
    }
  }

  Future<void> _checkRunningTask() async {
    if (Platform.isIOS) {
      // On iOS, check if our tracker service is running
      final isRunning = IOSTrackerService.instance.isRunning;
      if (isRunning) {
        setState(() {
          _isTracking = true;
        });
      } else if (widget.prefs.trackingActive) {
        // Was tracking but stopped - restart with permission checks
        WidgetsBinding.instance.addPostFrameCallback((_) {
          _checkPermissionsAndStart();
        });
      }
    } else {
      // On Android, check the foreground task service
      final isRunning = await FlutterForegroundTask.isRunningService;
      if (isRunning) {
        setState(() {
          _isTracking = true;
        });
        // Re-send config to task in case it needs it
        _sendConfigToTask();
      } else if (widget.prefs.trackingActive) {
        // Was tracking but task stopped - restart it with permission checks
        // (permissions may have been revoked while app was closed)
        WidgetsBinding.instance.addPostFrameCallback((_) {
          _checkPermissionsAndStart();
        });
      }
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    FlutterForegroundTask.removeTaskDataCallback(_onReceiveTaskData);
    super.dispose();
  }

  void _onReceiveTaskData(Object data) {
    debugPrint('Received data from task: $data');
    if (data is Map<String, dynamic>) {
      final type = data['type'] as String?;

      if (type == 'location') {
        setState(() {
          _lastPosition = TrackerPosition(
            latitude: data['latitude'] as double,
            longitude: data['longitude'] as double,
            speedKnots: data['speedKnots'] as double,
            heading: data['heading'] as int,
            timestamp: DateTime.fromMillisecondsSinceEpoch(data['timestamp'] as int),
          );
          _lastUpdateTime = DateFormat('HH:mm:ss').format(DateTime.now());
        });
      } else if (type == 'ack') {
        setState(() {
          _lastAckSeq = data['seq'] as int;
          _ackRate = (data['ackRate'] as num).toDouble();
          // Update event name if present in ACK
          final eventName = data['eventName'] as String?;
          if (eventName != null && eventName.isNotEmpty) {
            _eventName = eventName;
          }
        });
      } else if (type == 'packetSent') {
        // Could show packet sent indicator if desired
      } else if (type == 'error') {
        _showMessage(data['message'] as String? ?? 'Unknown error');
      } else if (type == 'authError') {
        _showMessage('Authentication error: ${data['message'] as String? ?? 'Invalid password'}');
      }
    }
  }

  Future<void> _checkPermissionsAndStart() async {
    // Validate ID and password before starting
    final sailorId = widget.prefs.sailorId.trim();
    final password = widget.prefs.password;

    if (sailorId.isEmpty && password.isEmpty) {
      _showMessage('Sailor ID and password are required. Please configure in Settings.');
      return;
    }
    if (sailorId.isEmpty) {
      _showMessage('Sailor ID is required. Please configure in Settings.');
      return;
    }
    if (password.isEmpty) {
      _showMessage('Password is required. Please configure in Settings.');
      return;
    }

    // Step 1: Check if location services are enabled
    bool serviceEnabled = await Geolocator.isLocationServiceEnabled();
    if (!serviceEnabled) {
      _showMessage('Please enable location services');
      return;
    }

    // Step 2: Check fine location permission
    LocationPermission permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) {
      permission = await Geolocator.requestPermission();
      if (permission == LocationPermission.denied) {
        _showMessage('Location permission required for tracking');
        return;
      }
    }

    if (permission == LocationPermission.deniedForever) {
      _showMessage('Location permission permanently denied. Please enable in settings.');
      await openAppSettings();
      return;
    }

    // Step 3: Check background location permission (Android and iOS)
    // On iOS, Geolocator.checkPermission() already returns .always or .whileInUse
    // On Android, we need to check separately with permission_handler
    bool hasBackgroundPermission = (permission == LocationPermission.always);

    if (!hasBackgroundPermission && Platform.isAndroid) {
      // Double-check with permission_handler on Android
      final bgStatus = await Permission.locationAlways.status;
      hasBackgroundPermission = bgStatus.isGranted;
    }

    print('Background location permission: $hasBackgroundPermission (geolocator: $permission)');

    if (!hasBackgroundPermission) {
      // Show explanation dialog first
      final instructionText = Platform.isIOS
          ? 'To track your position while the screen is off or app is in background, '
            'please select "Always" on the next screen.'
          : 'To track your position while the screen is off or app is in background, '
            'please select "Allow all the time" on the next screen.';

      final shouldRequest = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Background Location Required'),
          content: Text(instructionText),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(false),
              child: const Text('CANCEL'),
            ),
            TextButton(
              onPressed: () => Navigator.of(context).pop(true),
              child: const Text('CONTINUE'),
            ),
          ],
        ),
      );

      if (shouldRequest != true) {
        _showMessage('Background location required for tracking');
        return;
      }

      if (Platform.isIOS) {
        // On iOS, open settings since we can't request Always directly after initial grant
        await openAppSettings();
        _showMessage('Please set location to "Always", then try again.');
        return;
      } else {
        final result = await Permission.locationAlways.request();
        if (!result.isGranted) {
          _showMessage('Background location permission required. Please enable in settings.');
          return;
        }
      }
    }

    // Step 4: Request notification permission for foreground service (Android 13+)
    final notificationPermission =
        await FlutterForegroundTask.checkNotificationPermission();
    if (notificationPermission != NotificationPermission.granted) {
      await FlutterForegroundTask.requestNotificationPermission();
    }

    // Step 5: Check battery optimization (Android only, ask once)
    if (Platform.isAndroid && !widget.prefs.batteryOptAsked) {
      final isIgnoring = await Permission.ignoreBatteryOptimizations.isGranted;
      if (!isIgnoring) {
        final shouldRequest = await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('Battery Optimization'),
            content: const Text(
              'For reliable background tracking, this app should be excluded from '
              'battery optimization. This ensures position updates continue when '
              'the screen is off.',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.of(context).pop(false),
                child: const Text('SKIP'),
              ),
              TextButton(
                onPressed: () => Navigator.of(context).pop(true),
                child: const Text('ALLOW'),
              ),
            ],
          ),
        );

        // Remember that we asked (don't ask again)
        widget.prefs.batteryOptAsked = true;

        if (shouldRequest == true) {
          await Permission.ignoreBatteryOptimizations.request();
        }
      }
    }

    _startTracking();
  }

  Future<void> _sendConfigToTask() async {
    final prefs = widget.prefs;

    // Get version string with git hash if available
    String version = 'flutter';
    try {
      final packageInfo = await PackageInfo.fromPlatform();
      const gitHash = String.fromEnvironment('GIT_HASH', defaultValue: '');
      if (gitHash.isNotEmpty) {
        version = '${packageInfo.version}+${packageInfo.buildNumber}($gitHash)';
      } else {
        version = '${packageInfo.version}+${packageInfo.buildNumber}(flutter)';
      }
    } catch (e) {
      debugPrint('Failed to get package info: $e');
    }

    sendConfigToTask(
      serverHost: prefs.serverHost,
      serverPort: prefs.serverPort,
      sailorId: prefs.sailorId,
      role: prefs.role,
      password: prefs.password,
      eventId: prefs.eventId,
      version: version,
      highFrequencyMode: prefs.highFrequencyMode,
    );
  }

  Future<void> _startTracking() async {
    // Clear event name - will be repopulated from first ACK
    setState(() {
      _eventName = '';
    });

    final prefs = widget.prefs;

    if (Platform.isIOS) {
      // On iOS, use the direct tracker service (task handler doesn't work)
      await _startIOSTracking();
    } else {
      // On Android, use the foreground task service
      final result = await startForegroundTracking(
        serverHost: prefs.serverHost,
        serverPort: prefs.serverPort,
        sailorId: prefs.sailorId,
        role: prefs.role,
        password: prefs.password,
      );

      if (result is ServiceRequestSuccess) {
        await _sendConfigToTask();
        setState(() {
          _isTracking = true;
        });
        prefs.trackingActive = true;
      } else {
        _showMessage('Failed to start tracking: $result');
        return;
      }
    }
  }

  /// Start iOS-specific tracking service
  Future<void> _startIOSTracking() async {
    final prefs = widget.prefs;
    final iosTracker = IOSTrackerService.instance;

    // Set up callback to receive data
    iosTracker.onDataReceived = _onReceiveTaskData;

    // Configure the tracker
    await iosTracker.configure(
      serverHost: prefs.serverHost,
      serverPort: prefs.serverPort,
      sailorId: prefs.sailorId,
      role: prefs.role,
      password: prefs.password,
      eventId: prefs.eventId,
      highFrequencyMode: prefs.highFrequencyMode,
    );

    // Start tracking
    await iosTracker.start();

    setState(() {
      _isTracking = true;
    });
    prefs.trackingActive = true;

    _showMessage('iOS tracking started');
  }

  Future<void> _stopTracking() async {
    if (Platform.isIOS) {
      await IOSTrackerService.instance.stop();
    } else {
      await stopForegroundTracking();
    }

    widget.prefs.trackingActive = false;
    WakelockPlus.disable();

    setState(() {
      _isTracking = false;
      _assistActive = false;
    });
  }

  void _showStopConfirmation() {
    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Stop Tracking?'),
        content: const Text(
            'Are you sure you want to stop tracking? Your position will no longer be reported.'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            style: TextButton.styleFrom(
              foregroundColor: Colors.black,
              backgroundColor: const Color(0xFFCCCCCC),
            ),
            child: const Text('CANCEL', style: TextStyle(fontSize: 16)),
          ),
          TextButton(
            onPressed: () {
              Navigator.of(context).pop();
              _stopTracking();
            },
            style: TextButton.styleFrom(
              foregroundColor: Colors.white,
              backgroundColor: const Color(0xFFCC0000),
            ),
            child: const Text('STOP', style: TextStyle(fontSize: 16)),
          ),
        ],
      ),
    );
  }

  void _toggleAssist() {
    if (!_isTracking) {
      _showMessage('Start tracking first');
      return;
    }

    final newState = !_assistActive;

    // Send to appropriate service based on platform
    if (Platform.isIOS) {
      IOSTrackerService.instance.setAssistRequested(newState);
    } else {
      sendAssistToTask(newState);
    }

    setState(() {
      _assistActive = newState;
    });

    // Vibration feedback
    if (newState) {
      Vibration.vibrate(pattern: [0, 300, 100, 300]);
      _showMessage('ASSIST REQUEST ACTIVATED');
      WakelockPlus.enable();
    } else {
      Vibration.vibrate(duration: 100);
      _showMessage('Assist request cancelled');
      WakelockPlus.disable();
    }
  }

  void _onAssistTap() {
    if (!_isTracking) {
      _showMessage('Start tracking first');
    } else if (_assistActive) {
      _showMessage('Long press to CANCEL assist request');
    } else {
      _showMessage('Long press to request assistance');
    }
  }

  void _showSettings() async {
    // Save old values to detect changes
    final oldSailorId = widget.prefs.sailorId;
    final oldServerHost = widget.prefs.serverHost;
    final oldServerPort = widget.prefs.serverPort;
    final oldRole = widget.prefs.role;
    final oldPassword = widget.prefs.password;
    final oldEventId = widget.prefs.eventId;
    final oldHighFrequencyMode = widget.prefs.highFrequencyMode;

    final saved = await SettingsDialog.show(context, widget.prefs);
    if (saved == true) {
      // Check if any settings changed while tracking
      final settingsChanged = widget.prefs.sailorId != oldSailorId ||
          widget.prefs.serverHost != oldServerHost ||
          widget.prefs.serverPort != oldServerPort ||
          widget.prefs.role != oldRole ||
          widget.prefs.password != oldPassword ||
          widget.prefs.eventId != oldEventId ||
          widget.prefs.highFrequencyMode != oldHighFrequencyMode;

      if (_isTracking && settingsChanged) {
        _showMessage('Restarting tracking with new settings...');
        await _stopTracking();
        await Future.delayed(const Duration(milliseconds: 500));
        _startTracking();
      } else {
        _showMessage('Settings saved');
      }
    }
  }

  void _showMessage(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message)),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.white,
      appBar: AppBar(
        title: const Text('Windsurfer Tracker'),
        backgroundColor: const Color(0xFF0066CC),
        foregroundColor: Colors.white,
        actions: [
          IconButton(
            icon: const Icon(Icons.settings),
            onPressed: _showSettings,
          ),
        ],
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: _isTracking ? _buildTrackingView() : _buildConfigView(),
        ),
      ),
    );
  }

  Widget _buildConfigView() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _buildConfigField('Your Name', widget.prefs.sailorId),
        const SizedBox(height: 12),
        _buildConfigField('Server', widget.prefs.serverHost),
        const SizedBox(height: 12),
        _buildConfigField('Port', widget.prefs.serverPort.toString()),
        const Spacer(),
        ElevatedButton(
          onPressed: _checkPermissionsAndStart,
          style: ElevatedButton.styleFrom(
            backgroundColor: const Color(0xFF00AA00),
            foregroundColor: Colors.white,
            padding: const EdgeInsets.symmetric(vertical: 20),
            textStyle: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold),
          ),
          child: const Text('Start Tracking'),
        ),
      ],
    );
  }

  Widget _buildConfigField(String label, String value) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: const TextStyle(
            fontSize: 14,
            fontWeight: FontWeight.bold,
            color: Colors.black,
          ),
        ),
        const SizedBox(height: 4),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: const Color(0xFFEEEEEE),
            borderRadius: BorderRadius.circular(4),
          ),
          child: Text(
            value,
            style: const TextStyle(fontSize: 16, color: Colors.black),
          ),
        ),
      ],
    );
  }

  Widget _buildTrackingView() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _buildStatusSection(),
        const SizedBox(height: 20),
        AssistButton(
          isActive: _assistActive,
          isEnabled: _isTracking,
          onLongPress: _toggleAssist,
          onTap: _onAssistTap,
        ),
        const Spacer(),
        ElevatedButton(
          onPressed: _showStopConfirmation,
          style: ElevatedButton.styleFrom(
            backgroundColor: const Color(0xFFCC0000),
            foregroundColor: Colors.white,
            padding: const EdgeInsets.symmetric(vertical: 20),
            textStyle: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold),
          ),
          child: const Text('Stop Tracking'),
        ),
      ],
    );
  }

  Widget _buildStatusSection() {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: const Color(0xFFF5F5F5),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: const Color(0xFFDDDDDD)),
      ),
      child: Column(
        children: [
          // Event name and 1Hz mode indicator row
          if (_eventName.isNotEmpty || widget.prefs.highFrequencyMode)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Wrap(
                spacing: 8,
                runSpacing: 4,
                alignment: WrapAlignment.center,
                children: [
                  if (_eventName.isNotEmpty)
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                      decoration: BoxDecoration(
                        color: const Color(0xFF0066CC),
                        borderRadius: BorderRadius.circular(4),
                      ),
                      child: Text(
                        _eventName,
                        style: const TextStyle(
                          fontSize: 14,
                          fontWeight: FontWeight.bold,
                          color: Colors.white,
                        ),
                      ),
                    ),
                  if (widget.prefs.highFrequencyMode)
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
                      decoration: BoxDecoration(
                        color: const Color(0xFF00AAAA),
                        borderRadius: BorderRadius.circular(4),
                      ),
                      child: const Text(
                        '1Hz MODE',
                        style: TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.bold,
                          color: Colors.white,
                        ),
                      ),
                    ),
                ],
              ),
            ),
          _buildStatusRow('Position', _formatPosition()),
          const Divider(),
          Row(
            children: [
              Expanded(child: _buildStatusRow('Speed', _formatSpeed())),
              Expanded(child: _buildStatusRow('Heading', _formatHeading())),
            ],
          ),
          const Divider(),
          Row(
            children: [
              Expanded(child: _buildStatusRow('ACK Rate', _formatAckRate())),
              Expanded(child: _buildStatusRow('Last ACK', '#$_lastAckSeq')),
            ],
          ),
          const Divider(),
          _buildStatusRow('Last Update', _lastUpdateTime),
        ],
      ),
    );
  }

  Widget _buildStatusRow(String label, String value, {Color? valueColor}) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(
            label,
            style: const TextStyle(
              fontSize: 14,
              color: Color(0xFF666666),
            ),
          ),
          Text(
            value,
            style: TextStyle(
              fontSize: 16,
              fontWeight: FontWeight.bold,
              color: valueColor ?? Colors.black,
            ),
          ),
        ],
      ),
    );
  }

  String _formatPosition() {
    final pos = _lastPosition;
    if (pos == null) return '-- --';

    final latDir = pos.latitude < 0 ? 'S' : 'N';
    final lonDir = pos.longitude < 0 ? 'W' : 'E';
    return '${pos.latitude.abs().toStringAsFixed(5)}°$latDir ${pos.longitude.abs().toStringAsFixed(5)}°$lonDir';
  }

  String _formatSpeed() {
    final pos = _lastPosition;
    if (pos == null) return '-- kn';
    return '${pos.speedKnots.toStringAsFixed(1)} kn';
  }

  String _formatHeading() {
    final pos = _lastPosition;
    if (pos == null) return '---°';
    return '${pos.heading.toString().padLeft(3, '0')}°';
  }

  String _formatAckRate() {
    final percentage = (_ackRate * 100).round().clamp(0, 100);
    return '$percentage%';
  }
}
