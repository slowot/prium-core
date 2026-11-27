// Copyright 2026 Ant-Browser Authors. All rights reserved.
#include "base/ant_chromium/ant_shm_config.h"

#include <algorithm>
#include <array>
#include <cstring>
#include <string_view>

#include "base/compiler_specific.h"
#include "base/containers/span.h"
#include "build/build_config.h"

#if BUILDFLAG(IS_WIN)
#include <windows.h>
#endif

namespace ant_chromium {

namespace {

uint32_t ComputeCrc32Ieee(base::span<const uint8_t> data) {
  uint32_t crc = 0xFFFFFFFFu;
  for (uint8_t byte : data) {
    crc ^= byte;
    for (int bit = 0; bit < 8; ++bit) {
      uint32_t mask = (crc & 1u) ? 0xFFFFFFFFu : 0u;
      crc = (crc >> 1) ^ (0xEDB88320u & mask);
    }
  }
  return ~crc;
}

template <size_t N>
std::string ReadFixedUtf8(const char (&buf)[N]) {
  std::string_view sv(buf, N);
  return std::string(sv.substr(0, sv.find('\0')));
}

}  // namespace

// static
const AntShmConfig& AntShmConfig::Get() {
  static const base::NoDestructor<AntShmConfig> instance;
  return *instance;
}

AntShmConfig::AntShmConfig() {
  LoadFromEnvironment();
}

AntShmConfig::~AntShmConfig() {
#if BUILDFLAG(IS_WIN)
  ::SecureZeroMemory(vault_master_key_.data(), vault_master_key_.size());
  ::SecureZeroMemory(vault_key_id_.data(), vault_key_id_.size());
#endif
}

void AntShmConfig::LoadFromEnvironment() {
#if BUILDFLAG(IS_WIN)
  wchar_t shm_name[512] = {};
  DWORD env_len =
      ::GetEnvironmentVariableW(L"ANT_CHROMIUM_SHM_KEY", shm_name, 512);
  if (env_len == 0 || env_len >= 512) {
    return;
  }

  HANDLE mapping = ::OpenFileMappingW(FILE_MAP_READ, FALSE, shm_name);
  ::SecureZeroMemory(shm_name, sizeof(shm_name));
  if (!mapping) {
    return;
  }

  const void* view =
      ::MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, kAntShmPayloadSize);
  if (!view) {
    ::CloseHandle(mapping);
    return;
  }

  std::array<uint8_t, kAntShmPayloadSize> raw_bytes{};
  UNSAFE_TODO(std::memcpy(raw_bytes.data(), view, kAntShmPayloadSize));
  ::UnmapViewOfFile(view);
  ::CloseHandle(mapping);

  AntKernelShmPayload local_copy{};
  UNSAFE_TODO(std::memcpy(&local_copy, raw_bytes.data(),
                          sizeof(AntKernelShmPayload)));

  const bool magic_ok = std::ranges::equal(local_copy.magic, kAntShmMagic);
  const bool version_ok = local_copy.version == kAntShmVersion;
  const bool size_ok = local_copy.payload_size == kAntShmPayloadSize;

  const uint32_t expected_crc = ComputeCrc32Ieee(
      base::span<const uint8_t>(raw_bytes).subspan(size_t{20}));
  ::SecureZeroMemory(raw_bytes.data(), raw_bytes.size());

  if (!magic_ok || !version_ok || !size_ok ||
      local_copy.crc32 != expected_crc) {
    ::SecureZeroMemory(&local_copy, sizeof(local_copy));
    return;
  }

  seed_ = local_copy.seed;
  hardware_concurrency_ =
      std::max<uint32_t>(1u, local_copy.hardware_concurrency);
  device_memory_ = std::clamp<uint32_t>(local_copy.device_memory, 1u, 8u);
  feature_flags_ = local_copy.feature_flags;
  max_touch_points_ = local_copy.max_touch_points;
  screen_width_ =
      local_copy.screen_width > 0 ? local_copy.screen_width : 1920u;
  screen_height_ =
      local_copy.screen_height > 0 ? local_copy.screen_height : 1080u;
  device_pixel_ratio_ =
      local_copy.device_pixel_ratio > 0.0 ? local_copy.device_pixel_ratio : 1.0;

  profile_id_ = ReadFixedUtf8(local_copy.profile_id);
  user_agent_ = ReadFixedUtf8(local_copy.user_agent);
  platform_ = ReadFixedUtf8(local_copy.platform);
  webgl_vendor_ = ReadFixedUtf8(local_copy.webgl_vendor);
  webgl_renderer_ = ReadFixedUtf8(local_copy.webgl_renderer);
  timezone_ = ReadFixedUtf8(local_copy.timezone);
  locale_ = ReadFixedUtf8(local_copy.locale);
  webrtc_policy_ = ReadFixedUtf8(local_copy.webrtc_policy);
  vault_mode_ = local_copy.vault_mode;
  std::ranges::copy(local_copy.vault_master_key, vault_master_key_.begin());
  std::ranges::copy(local_copy.vault_key_id, vault_key_id_.begin());
  loaded_ = true;

  ::SecureZeroMemory(&local_copy, sizeof(local_copy));
#endif
}

}  // namespace ant_chromium
