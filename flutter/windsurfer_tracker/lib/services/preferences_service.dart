import 'package:shared_preferences/shared_preferences.dart';
import 'foreground_task_handler.dart';

/// Preferences keys
class PrefsKeys {
  static const String sailorId = 'sailor_id';
  static const String serverHost = 'server_host';
  static const String serverPort = 'server_port';
  static const String role = 'role';
  static const String password = 'password';
  static const String eventId = 'event_id';
  static const String trackingActive = 'tracking_active';
  static const String batteryOptAsked = 'battery_opt_asked';
  static const String highFrequencyMode = 'high_frequency_mode';
}

/// Service for managing app preferences
class PreferencesService {
  SharedPreferences? _prefs;

  Future<void> init() async {
    _prefs = await SharedPreferences.getInstance();

    // Migrate old server address for early beta testers
    final host = _prefs!.getString(PrefsKeys.serverHost);
    if (host == 'track.tridgell.net') {
      await _prefs!.setString(PrefsKeys.serverHost, 'wstracker.org');
    }
  }

  SharedPreferences get _p {
    if (_prefs == null) {
      throw StateError('PreferencesService not initialized. Call init() first.');
    }
    return _prefs!;
  }

  // Sailor ID
  String get sailorId => _p.getString(PrefsKeys.sailorId) ?? '';
  set sailorId(String value) => _p.setString(PrefsKeys.sailorId, value);

  // Server host
  String get serverHost =>
      _p.getString(PrefsKeys.serverHost) ?? TrackerConfig.defaultServerHost;
  set serverHost(String value) => _p.setString(PrefsKeys.serverHost, value);

  // Server port
  int get serverPort =>
      _p.getInt(PrefsKeys.serverPort) ?? TrackerConfig.defaultServerPort;
  set serverPort(int value) => _p.setInt(PrefsKeys.serverPort, value);

  // Role
  String get role => _p.getString(PrefsKeys.role) ?? 'sailor';
  set role(String value) => _p.setString(PrefsKeys.role, value);

  // Password
  String get password => _p.getString(PrefsKeys.password) ?? '';
  set password(String value) => _p.setString(PrefsKeys.password, value);

  // Event ID (for multi-event support)
  int get eventId => _p.getInt(PrefsKeys.eventId) ?? 1;
  set eventId(int value) => _p.setInt(PrefsKeys.eventId, value);

  // Tracking active (for auto-resume)
  bool get trackingActive => _p.getBool(PrefsKeys.trackingActive) ?? false;
  set trackingActive(bool value) => _p.setBool(PrefsKeys.trackingActive, value);

  // Battery optimization asked (only ask once)
  bool get batteryOptAsked => _p.getBool(PrefsKeys.batteryOptAsked) ?? false;
  set batteryOptAsked(bool value) => _p.setBool(PrefsKeys.batteryOptAsked, value);

  // High frequency (1Hz) mode - send positions as array
  bool get highFrequencyMode => _p.getBool(PrefsKeys.highFrequencyMode) ?? false;
  set highFrequencyMode(bool value) => _p.setBool(PrefsKeys.highFrequencyMode, value);

  /// Save all settings at once
  Future<void> saveAll({
    required String sailorId,
    required String serverHost,
    required int serverPort,
    required String role,
    required String password,
    int eventId = 1,
    required bool highFrequencyMode,
  }) async {
    await _p.setString(PrefsKeys.sailorId, sailorId);
    await _p.setString(PrefsKeys.serverHost, serverHost);
    await _p.setInt(PrefsKeys.serverPort, serverPort);
    await _p.setString(PrefsKeys.role, role);
    await _p.setString(PrefsKeys.password, password);
    await _p.setInt(PrefsKeys.eventId, eventId);
    await _p.setBool(PrefsKeys.highFrequencyMode, highFrequencyMode);
  }
}
