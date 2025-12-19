import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:flutter/foundation.dart';

/// Event information from server
class EventInfo {
  final int eid;
  final String name;
  final String description;

  EventInfo({
    required this.eid,
    required this.name,
    required this.description,
  });

  factory EventInfo.fromJson(Map<String, dynamic> json) {
    return EventInfo(
      eid: json['eid'] as int,
      name: json['name'] as String,
      description: json['description'] as String? ?? '',
    );
  }
}

/// Service for fetching events from the server
class EventService {
  /// Fetch events list from server
  static Future<List<EventInfo>> fetchEvents(String serverHost, int serverPort) async {
    try {
      // For wstracker.org domains, always use HTTPS on port 443 (nginx proxy)
      final isWstracker = serverHost.contains('wstracker.org');
      final protocol = (serverPort == 443 || isWstracker) ? 'https' : 'http';
      final portSuffix = isWstracker ? '' : (
        (protocol == 'https' && serverPort == 443) ||
        (protocol == 'http' && serverPort == 80)
      ) ? '' : ':$serverPort';

      final url = Uri.parse('$protocol://$serverHost$portSuffix/api/events');
      debugPrint('Fetching events from: $url');

      final response = await http.get(url).timeout(
        const Duration(seconds: 10),
      );

      if (response.statusCode != 200) {
        debugPrint('Failed to fetch events: HTTP ${response.statusCode}');
        return [];
      }

      final json = jsonDecode(response.body) as Map<String, dynamic>;
      final eventsArray = json['events'] as List<dynamic>;

      final events = eventsArray.map((e) => EventInfo.fromJson(e as Map<String, dynamic>)).toList();
      debugPrint('Fetched ${events.length} events');
      return events;
    } catch (e) {
      debugPrint('Error fetching events: $e');
      return [];
    }
  }
}
