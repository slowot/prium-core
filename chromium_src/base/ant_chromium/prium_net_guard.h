// Copyright 2026 Ant-Browser Authors. All rights reserved.
#ifndef BASE_ANT_CHROMIUM_PRIUM_NET_GUARD_H_
#define BASE_ANT_CHROMIUM_PRIUM_NET_GUARD_H_

#include <cstdint>
#include <string_view>

#include "base/base_export.h"

namespace ant_chromium {

class BASE_EXPORT PriumNetGuard {
 public:
  // Double-guarded pre-HostResolver sinkhole:
  // 1) Never intercepts requests with an http(s) web page initiator or non-empty
  //    navigation/resource destination (leaving Google, YouTube, reCAPTCHA,
  //    OAuth, and HSTS 307 100% genuine-Chrome identical).
  // 2) Only blocks browser-internal background phone-home endpoints (UMA, UKM,
  //    Finch Variations fetch, SafeBrowsing pings, Component Updater, Gaia
  //    probes, OptimizationGuide, Crashpad upload).
  static bool ShouldSinkholeInternalRequest(bool has_web_initiator,
                                            int32_t traffic_annotation_hash,
                                            std::string_view url);
  static uint64_t GetSinkholedCount();
};

}  // namespace ant_chromium

#endif  // BASE_ANT_CHROMIUM_PRIUM_NET_GUARD_H_
