// Copyright 2026 Ant-Browser Authors. All rights reserved.
#ifndef BASE_ANT_CHROMIUM_ANT_BLACKBOX_LOGGER_H_
#define BASE_ANT_CHROMIUM_ANT_BLACKBOX_LOGGER_H_

#include <string>
#include <string_view>

#include "base/base_export.h"
#include "base/no_destructor.h"

namespace ant_chromium {

inline constexpr char kAntBlackboxEnvKey[] = "ANT_CHROMIUM_BLACKBOX_LOG";

class BASE_EXPORT AntBlackboxLogger {
 public:
  static AntBlackboxLogger& Get();

  bool is_enabled() const { return enabled_; }
  void EmitEvent(std::string_view event_name, std::string_view detail) const;
  static std::string RedactSensitive(std::string_view input);

 private:
  friend class base::NoDestructor<AntBlackboxLogger>;
  AntBlackboxLogger();
  bool enabled_ = false;
  std::string log_path_;
};

}  // namespace ant_chromium

#endif  // BASE_ANT_CHROMIUM_ANT_BLACKBOX_LOGGER_H_
