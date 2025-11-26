import 'package:flutter/material.dart';

/// Large assist button with long-press activation and pulsing animation
class AssistButton extends StatefulWidget {
  final bool isActive;
  final bool isEnabled;
  final VoidCallback onLongPress;
  final VoidCallback onTap;

  const AssistButton({
    super.key,
    required this.isActive,
    required this.isEnabled,
    required this.onLongPress,
    required this.onTap,
  });

  @override
  State<AssistButton> createState() => _AssistButtonState();
}

class _AssistButtonState extends State<AssistButton>
    with SingleTickerProviderStateMixin {
  late AnimationController _pulseController;
  late Animation<double> _pulseAnimation;

  @override
  void initState() {
    super.initState();
    _pulseController = AnimationController(
      duration: const Duration(milliseconds: 500),
      vsync: this,
    );
    _pulseAnimation = Tween<double>(begin: 1.0, end: 0.7).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );
  }

  @override
  void didUpdateWidget(AssistButton oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.isActive && !oldWidget.isActive) {
      _startPulsing();
    } else if (!widget.isActive && oldWidget.isActive) {
      _stopPulsing();
    }
  }

  void _startPulsing() {
    _pulseController.repeat(reverse: true);
  }

  void _stopPulsing() {
    _pulseController.stop();
    _pulseController.value = 0;
  }

  @override
  void dispose() {
    _pulseController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final backgroundColor = widget.isActive
        ? const Color(0xFFFF0000) // Bright red
        : const Color(0xFF00AA00); // Bright green

    final textColor = widget.isActive
        ? const Color(0xFFFFFFFF) // White
        : const Color(0xFF000000); // Black

    final text = widget.isActive
        ? '⚠ ASSISTANCE REQUESTED ⚠\n\nLong press to cancel'
        : 'REQUEST ASSISTANCE\n\nLong press to activate';

    return AnimatedBuilder(
      animation: _pulseAnimation,
      builder: (context, child) {
        return Opacity(
          opacity: widget.isActive ? _pulseAnimation.value : 1.0,
          child: child,
        );
      },
      child: GestureDetector(
        onLongPress: widget.isEnabled ? widget.onLongPress : null,
        onTap: widget.onTap,
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(vertical: 30),
          decoration: BoxDecoration(
            color: backgroundColor,
            borderRadius: BorderRadius.circular(12),
          ),
          child: Text(
            text,
            textAlign: TextAlign.center,
            style: TextStyle(
              color: textColor,
              fontSize: 18,
              fontWeight: FontWeight.bold,
            ),
          ),
        ),
      ),
    );
  }
}
