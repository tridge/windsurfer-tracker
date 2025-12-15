import 'package:flutter/material.dart';
import 'package:package_info_plus/package_info_plus.dart';
import '../services/preferences_service.dart';
import '../services/tracker_service.dart';

/// Settings dialog matching Android app design
class SettingsDialog extends StatefulWidget {
  final PreferencesService prefs;

  const SettingsDialog({super.key, required this.prefs});

  @override
  State<SettingsDialog> createState() => _SettingsDialogState();

  static Future<bool?> show(BuildContext context, PreferencesService prefs) {
    return showDialog<bool>(
      context: context,
      builder: (context) => SettingsDialog(prefs: prefs),
    );
  }
}

class _SettingsDialogState extends State<SettingsDialog> {
  late TextEditingController _sailorIdController;
  late TextEditingController _serverHostController;
  late TextEditingController _serverPortController;
  late TextEditingController _passwordController;
  late String _selectedRole;
  bool _showPassword = false;
  bool _highFrequencyMode = false;
  String _versionString = '';

  final _roleOptions = ['Sailor', 'Support', 'Spectator'];
  final _roleValues = ['sailor', 'support', 'spectator'];

  @override
  void initState() {
    super.initState();
    _sailorIdController = TextEditingController(text: widget.prefs.sailorId);
    _serverHostController = TextEditingController(text: widget.prefs.serverHost);
    _serverPortController =
        TextEditingController(text: widget.prefs.serverPort.toString());
    _passwordController = TextEditingController(text: widget.prefs.password);
    _selectedRole = widget.prefs.role;
    if (!_roleValues.contains(_selectedRole)) {
      _selectedRole = 'sailor';
    }
    _highFrequencyMode = widget.prefs.highFrequencyMode;
    _loadVersionInfo();
  }

  Future<void> _loadVersionInfo() async {
    final packageInfo = await PackageInfo.fromPlatform();
    const gitHash = String.fromEnvironment('GIT_HASH', defaultValue: '');
    final version = '${packageInfo.version}+${packageInfo.buildNumber}';
    if (mounted) {
      setState(() {
        _versionString = gitHash.isNotEmpty ? '$version ($gitHash)' : version;
      });
    }
  }

  @override
  void dispose() {
    _sailorIdController.dispose();
    _serverHostController.dispose();
    _serverPortController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Settings'),
      backgroundColor: Colors.white,
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _buildLabel('ID'),
            _buildTextField(_sailorIdController, 'Sailor ID'),
            const SizedBox(height: 16),
            _buildLabel('Role'),
            _buildRoleDropdown(),
            const SizedBox(height: 16),
            _buildLabel('Server Address'),
            _buildTextField(
              _serverHostController,
              'IP or hostname',
              keyboardType: TextInputType.url,
              autocorrect: false,
            ),
            const SizedBox(height: 16),
            _buildLabel('Server Port'),
            _buildTextField(
              _serverPortController,
              'Port',
              keyboardType: TextInputType.number,
            ),
            const SizedBox(height: 16),
            _buildLabel('Password'),
            _buildPasswordField(),
            _buildShowPasswordCheckbox(),
            const SizedBox(height: 16),
            const Divider(),
            const SizedBox(height: 8),
            _buildHighFrequencyCheckbox(),
            const SizedBox(height: 16),
            _buildVersionInfo(),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(false),
          style: TextButton.styleFrom(
            foregroundColor: Colors.black,
            backgroundColor: const Color(0xFFCCCCCC),
            padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
          ),
          child: const Text('CANCEL', style: TextStyle(fontSize: 16)),
        ),
        TextButton(
          onPressed: _save,
          style: TextButton.styleFrom(
            foregroundColor: Colors.white,
            backgroundColor: const Color(0xFF00AA00),
            padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
          ),
          child: const Text('SAVE', style: TextStyle(fontSize: 16)),
        ),
      ],
    );
  }

  Widget _buildLabel(String text) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Text(
        text,
        style: const TextStyle(
          fontSize: 14,
          fontWeight: FontWeight.w500,
          color: Colors.black,
        ),
      ),
    );
  }

  Widget _buildTextField(
    TextEditingController controller,
    String hint, {
    TextInputType keyboardType = TextInputType.text,
    bool autocorrect = true,
  }) {
    return TextField(
      controller: controller,
      keyboardType: keyboardType,
      autocorrect: autocorrect,
      enableSuggestions: autocorrect,
      style: const TextStyle(fontSize: 16, color: Colors.black),
      decoration: InputDecoration(
        hintText: hint,
        filled: true,
        fillColor: const Color(0xFFEEEEEE),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(4),
          borderSide: BorderSide.none,
        ),
        contentPadding:
            const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
      ),
    );
  }

  Widget _buildRoleDropdown() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12),
      decoration: BoxDecoration(
        color: const Color(0xFFEEEEEE),
        borderRadius: BorderRadius.circular(4),
      ),
      child: DropdownButton<String>(
        value: _selectedRole,
        isExpanded: true,
        underline: const SizedBox(),
        style: const TextStyle(fontSize: 16, color: Colors.black),
        items: List.generate(_roleOptions.length, (index) {
          return DropdownMenuItem(
            value: _roleValues[index],
            child: Text(_roleOptions[index]),
          );
        }),
        onChanged: (value) {
          if (value != null) {
            setState(() => _selectedRole = value);
          }
        },
      ),
    );
  }

  Widget _buildPasswordField() {
    return TextField(
      controller: _passwordController,
      obscureText: !_showPassword,
      style: const TextStyle(fontSize: 16, color: Colors.black),
      decoration: InputDecoration(
        hintText: 'Password',
        filled: true,
        fillColor: const Color(0xFFEEEEEE),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(4),
          borderSide: BorderSide.none,
        ),
        contentPadding:
            const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
      ),
    );
  }

  Widget _buildShowPasswordCheckbox() {
    return Row(
      children: [
        Checkbox(
          value: _showPassword,
          onChanged: (value) {
            setState(() => _showPassword = value ?? false);
          },
        ),
        const Text(
          'Show password',
          style: TextStyle(fontSize: 14, color: Colors.black),
        ),
      ],
    );
  }

  Widget _buildHighFrequencyCheckbox() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Checkbox(
              value: _highFrequencyMode,
              onChanged: (value) {
                setState(() => _highFrequencyMode = value ?? false);
              },
            ),
            const Text(
              '1Hz Mode',
              style: TextStyle(fontSize: 14, color: Colors.black),
            ),
          ],
        ),
        const Padding(
          padding: EdgeInsets.only(left: 48),
          child: Text(
            'Send positions at 1Hz as batched arrays. Higher battery usage.',
            style: TextStyle(fontSize: 12, color: Color(0xFF666666)),
          ),
        ),
      ],
    );
  }

  Widget _buildVersionInfo() {
    return Center(
      child: Text(
        _versionString.isEmpty ? 'Loading...' : 'Version: $_versionString',
        style: const TextStyle(
          fontSize: 12,
          color: Color(0xFF888888),
        ),
      ),
    );
  }

  void _save() {
    // Validate required fields
    final sailorId = _sailorIdController.text.trim();
    final password = _passwordController.text;

    if (sailorId.isEmpty && password.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Sailor ID and password are required')),
      );
      return;
    }
    if (sailorId.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Sailor ID is required')),
      );
      return;
    }
    if (password.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Password is required')),
      );
      return;
    }

    final port = int.tryParse(_serverPortController.text) ??
        TrackerConfig.defaultServerPort;

    widget.prefs.saveAll(
      sailorId: sailorId,
      serverHost: _serverHostController.text,
      serverPort: port,
      role: _selectedRole,
      password: password,
      highFrequencyMode: _highFrequencyMode,
    );

    Navigator.of(context).pop(true);
  }
}
