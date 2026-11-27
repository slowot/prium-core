// Copyright 2026 Ant-Browser Authors. All rights reserved.
#include "base/ant_chromium/prium_net_guard.h"

#include <array>
#include <atomic>

#include "base/ant_chromium/ant_blackbox_logger.h"
#include "base/strings/string_util.h"

namespace ant_chromium {

namespace {

std::atomic<uint64_t> g_sinkholed_count{0};

constexpr std::array<std::string_view, 14> kInternalTelemetryUrls = {
    "clientservices.googleapis.com/chrome-variations",
    "update.googleapis.com/service/update2",
    "clients2.google.com/service/update2",
    "safebrowsing.googleapis.com",
    "sb-ssl.google.com",
    "ssl.gstatic.com/safebrowsing",
    "beacons.gcp.gvt2.com",
    "beacons.gvt2.com",
    "clients4.google.com/chrome-sync",
    "optimizationguide-pa.googleapis.com",
    "crash2.googleapis.com",
    "clients2.google.com/cr/report",
    "tools.google.com/service/update2",
    "chromesync.googleapis.com",
};

}  // namespace

// static
bool PriumNetGuard::ShouldSinkholeInternalRequest(
    bool has_web_initiator,
    int32_t traffic_annotation_hash,
    std::string_view url) {
  (void)traffic_annotation_hash;
  if (has_web_initiator) {
    return false;
  }
  std::string lower = base::ToLowerASCII(url);
  if (!base::StartsWith(lower, "https://") &&
      !base::StartsWith(lower, "http://")) {
    return false;
  }
  for (std::string_view pattern : kInternalTelemetryUrls) {
    if (lower.find(pattern) != std::string::npos) {
      g_sinkholed_count.fetch_add(1u, std::memory_order_relaxed);
      AntBlackboxLogger::Get().EmitEvent("netguard_sinkhole", pattern);
      return true;
    }
  }
  return false;
}

// static
uint64_t PriumNetGuard::GetSinkholedCount() {
  return g_sinkholed_count.load(std::memory_order_relaxed);
}

}  // namespace ant_chromium
