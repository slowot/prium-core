// Copyright 2026 Ant-Browser Authors. All rights reserved.
#ifndef BASE_ANT_CHROMIUM_ANT_SHM_CONFIG_H_
#define BASE_ANT_CHROMIUM_ANT_SHM_CONFIG_H_

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

#include "base/base_export.h"
#include "base/no_destructor.h"

namespace ant_chromium {

inline constexpr char kAntKernelVersionMarker[] =
    "AntChromiumKernel/150.0.7871.186 ant-chromium-shm-v1";
inline constexpr char kAntShmEnvKey[] = "ANT_CHROMIUM_SHM_KEY";
// 8-byte ASCII magic "ANTKRN01" (C-string literal "ANTKRN01\0" prefix).
inline constexpr char kAntShmMagic[8] = {'A', 'N', 'T', 'K', 'R', 'N', '0', '1'};
inline constexpr uint32_t kAntShmVersion = 1;
inline constexpr size_t kAntShmPayloadSize = 4096;

enum AntFeatureFlags : uint32_t {
  ANT_FEAT_CANVAS_NOISE = 1u << 0,
  ANT_FEAT_WEBGL_SPOOF = 1u << 1,
  ANT_FEAT_AUDIO_NOISE = 1u << 2,
  ANT_FEAT_FONT_SPOOF = 1u << 3,
  ANT_FEAT_WEBRTC_STEALTH = 1u << 4,
  ANT_FEAT_CLIENT_HINTS = 1u << 5,
  ANT_FEAT_DOMRECT_NOISE = 1u << 6,
  ANT_FEAT_CDP_STEALTH = 1u << 7,
  ANT_FEAT_VAULT_KEY = 1u << 8,
};

#pragma pack(push, 8)
struct AntKernelShmPayload {
  char magic[8];
  uint32_t version;
  uint32_t payload_size;
  uint32_t crc32;
  uint32_t reserved0;
  uint64_t seed;
  uint32_t hardware_concurrency;
  uint32_t device_memory;
  uint32_t feature_flags;
  uint32_t max_touch_points;
  uint32_t screen_width;
  uint32_t screen_height;
  double device_pixel_ratio;
  double geo_latitude;
  double geo_longitude;
  double geo_accuracy;
  uint64_t reserved1;
  char profile_id[128];
  char user_agent[512];
  char platform[64];
  char webgl_vendor[256];
  char webgl_renderer[512];
  char timezone[128];
  char locale[128];
  char webrtc_policy[64];
  uint32_t vault_mode;
  uint8_t vault_master_key[32];
  uint8_t vault_key_id[16];
  uint8_t reserved_tail[2156];
};
#pragma pack(pop)

static_assert(sizeof(AntKernelShmPayload) == kAntShmPayloadSize,
              "AntKernelShmPayload must be strictly 4096 bytes");
static_assert(offsetof(AntKernelShmPayload, seed) == 24,
              "seed offset mismatch");
static_assert(offsetof(AntKernelShmPayload, profile_id) == 96,
              "profile_id offset mismatch");
static_assert(offsetof(AntKernelShmPayload, user_agent) == 224,
              "user_agent offset mismatch");
static_assert(offsetof(AntKernelShmPayload, platform) == 736,
              "platform offset mismatch");
static_assert(offsetof(AntKernelShmPayload, webgl_vendor) == 800,
              "webgl_vendor offset mismatch");
static_assert(offsetof(AntKernelShmPayload, webgl_renderer) == 1056,
              "webgl_renderer offset mismatch");
static_assert(offsetof(AntKernelShmPayload, vault_mode) == 1888,
              "vault_mode offset mismatch");
static_assert(offsetof(AntKernelShmPayload, vault_master_key) == 1892,
              "vault_master_key offset mismatch");
static_assert(offsetof(AntKernelShmPayload, vault_key_id) == 1924,
              "vault_key_id offset mismatch");

class BASE_EXPORT AntShmConfig {
 public:
  static const AntShmConfig& Get();

  bool is_loaded() const { return loaded_; }
  uint64_t seed() const { return seed_; }
  uint32_t hardware_concurrency() const { return hardware_concurrency_; }
  uint32_t device_memory() const { return device_memory_; }
  uint32_t feature_flags() const { return feature_flags_; }
  bool HasFeature(AntFeatureFlags flag) const {
    return loaded_ && ((feature_flags_ & static_cast<uint32_t>(flag)) != 0);
  }
  uint32_t max_touch_points() const { return max_touch_points_; }
  uint32_t screen_width() const { return screen_width_; }
  uint32_t screen_height() const { return screen_height_; }
  double device_pixel_ratio() const { return device_pixel_ratio_; }
  const std::string& profile_id() const { return profile_id_; }
  const std::string& user_agent() const { return user_agent_; }
  const std::string& platform() const { return platform_; }
  const std::string& webgl_vendor() const { return webgl_vendor_; }
  const std::string& webgl_renderer() const { return webgl_renderer_; }
  const std::string& timezone() const { return timezone_; }
  const std::string& locale() const { return locale_; }
  const std::string& webrtc_policy() const { return webrtc_policy_; }
  uint32_t vault_mode() const { return vault_mode_; }
  const std::array<uint8_t, 32>& vault_master_key() const {
    return vault_master_key_;
  }
  const std::array<uint8_t, 16>& vault_key_id() const { return vault_key_id_; }

 private:
  friend class base::NoDestructor<AntShmConfig>;
  AntShmConfig();
  ~AntShmConfig();
  void LoadFromEnvironment();

  bool loaded_ = false;
  uint64_t seed_ = 0;
  uint32_t hardware_concurrency_ = 8;
  uint32_t device_memory_ = 8;
  uint32_t feature_flags_ = 0;
  uint32_t max_touch_points_ = 0;
  uint32_t screen_width_ = 1920;
  uint32_t screen_height_ = 1080;
  double device_pixel_ratio_ = 1.0;
  std::string profile_id_;
  std::string user_agent_;
  std::string platform_;
  std::string webgl_vendor_;
  std::string webgl_renderer_;
  std::string timezone_;
  std::string locale_;
  std::string webrtc_policy_;
  uint32_t vault_mode_ = 0;
  std::array<uint8_t, 32> vault_master_key_{};
  std::array<uint8_t, 16> vault_key_id_{};
};

}  // namespace ant_chromium

#endif  // BASE_ANT_CHROMIUM_ANT_SHM_CONFIG_H_
