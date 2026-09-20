#pragma once

class ScreenRecorder {
public:
  // Lifecycle and save commands run on the UI thread; capture and file writing run on workers.
  static void attach();  // call after the EGL display exists and before the first frame is swapped, when the swapchain is created
  static void start();
  static void stop();
  static bool active();
  static void setReplayDuration(int seconds);  // 0 disables replay; enabled durations are 30–300 seconds
  static int replaySeconds();
  static bool saveReplay();
  enum class ReplaySaveStatus { Idle, Saving, Saved, Failed };
  static ReplaySaveStatus replaySaveStatus();
  static void shutdown();
};
