#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <vector>

struct ReplayFrame {
  std::vector<uint8_t> data;
  int64_t timestamp_us;
  bool keyframe;
};

class ReplayBuffer {
public:
  static constexpr int64_t FRAME_INTERVAL_US = 1000000 / 30;
  // A save shares these frames; continued buffering can retain another MAX_BYTES.
  static constexpr size_t MAX_BYTES = 256ULL * 1024 * 1024;
  using Frame = std::shared_ptr<const ReplayFrame>;

  explicit ReplayBuffer(size_t byte_limit = MAX_BYTES) : max_bytes(byte_limit) {}

  void setDuration(int seconds) {
    duration_us = seconds * 1000000LL;
    if (!frame_groups.empty()) {
      trim(frame_groups.back().back()->timestamp_us);
    }
  }

  void clear() {
    frame_groups.clear();
    bytes = 0;
  }

  void append(const uint8_t *data, size_t size, int64_t timestamp_us, bool keyframe) {
    if (size > max_bytes) {
      clear();
      return;
    }
    while (bytes + size > max_bytes) {
      pop();
    }
    if (frame_groups.empty() && !keyframe) {
      return;
    }

    if (keyframe) {
      frame_groups.emplace_back();
    }
    frame_groups.back().push_back(std::make_shared<ReplayFrame>(ReplayFrame{{data, data + size}, timestamp_us, keyframe}));
    bytes += size;
    trim(timestamp_us);
  }

  std::vector<Frame> snapshot(int64_t start_us) const {
    size_t first = 0;
    while (first + 1 < frame_groups.size() && frame_groups[first + 1].front()->timestamp_us <= start_us) {
      first++;
    }

    size_t count = 0;
    for (size_t i = first; i < frame_groups.size(); i++) {
      count += frame_groups[i].size();
    }

    std::vector<Frame> frames;
    frames.reserve(count);
    for (size_t i = first; i < frame_groups.size(); i++) {
      frames.insert(frames.end(), frame_groups[i].begin(), frame_groups[i].end());
    }
    return frames;
  }

  int64_t end(int64_t timestamp_us) const {
    return frame_groups.empty() ? timestamp_us : std::min(timestamp_us, frame_groups.back().back()->timestamp_us + FRAME_INTERVAL_US);
  }

  int64_t start(int64_t timestamp_us) const {
    return frame_groups.empty() ? timestamp_us : std::max(frame_groups.front().front()->timestamp_us, timestamp_us - duration_us);
  }

  int seconds(int64_t timestamp_us) const {
    return (std::max<int64_t>(0, end(timestamp_us) - start(timestamp_us)) + 500000) / 1000000;
  }

private:
  void trim(int64_t timestamp_us) {
    // Retain the keyframe before the cutoff so the first visible frame can be decoded.
    const int64_t cutoff = timestamp_us - duration_us;
    while (frame_groups.size() > 1 && frame_groups[1].front()->timestamp_us <= cutoff) {
      pop();
    }
  }

  void pop() {
    for (const Frame &frame : frame_groups.front()) {
      bytes -= frame->data.size();
    }
    frame_groups.pop_front();
  }

  const size_t max_bytes;
  int64_t duration_us = 60LL * 1000000;
  size_t bytes = 0;
  std::deque<std::vector<Frame>> frame_groups;
};
