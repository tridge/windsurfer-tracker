import 'package:flutter/material.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'services/preferences_service.dart';
import 'services/foreground_task_handler.dart';
import 'screens/home_screen.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Initialize foreground task
  await initForegroundTask();

  final prefs = PreferencesService();
  await prefs.init();

  runApp(WindsurferTrackerApp(prefs: prefs));
}

class WindsurferTrackerApp extends StatelessWidget {
  final PreferencesService prefs;

  const WindsurferTrackerApp({super.key, required this.prefs});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Windsurfer Tracker',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF0066CC)),
        useMaterial3: true,
      ),
      // Wrap with WithForegroundTask for proper lifecycle management
      home: WithForegroundTask(
        child: HomeScreen(prefs: prefs),
      ),
    );
  }
}
