// Copyright 2026 Ant-Browser Authors. All rights reserved.
#include "base/ant_chromium/ant_blackbox_logger.h"

#include <array>
#include <chrono>

#include "base/no_destructor.h"
#include "base/strings/string_util.h"
#include "base/strings/utf_string_conversions.h"
#include "build/build_config.h"

#if BUILDFLAG(IS_WIN)
#include <windows.h>
#endif

namespace ant_chromium {

namespace {

std::string EscapeJsonString(std::string_view raw) {
  std::string out;
  out.reserve(raw.size() + 8);
  for (char ch : raw) {
    switch (ch) {
      case '"':
        out += "\\\"";
        break;
      case '\\':
        out += "\\\\";
        break;
      case '\n':
        out += "\\n";
        break;
      case '\r':
        out += "\\r";
        break;
      case '\t':
        out += "\\t";
        break;
      default:
        if (static_cast<unsigned char>(ch) >= 0x20) {
          out.push_back(ch);
        }
        break;
    }
  }
  return out;
}

}  // namespace

// static
AntBlackboxLogger& AntBlackboxLogger::Get() {
  static base::NoDestructor<AntBlackboxLogger> instance;
  return *instance;
}

AntBlackboxLogger::AntBlackboxLogger() {
#if BUILDFLAG(IS_WIN)
  wchar_t path_buf[1024] = {};
  DWORD len =
      ::GetEnvironmentVariableW(L"ANT_CHROMIUM_BLACKBOX_LOG", path_buf, 1024);
  if (len > 0 && len < 1024) {
    log_path_ = base::WideToUTF8(std::wstring_view(path_buf, len));
    enabled_ = !log_path_.empty();
  }
#endif
}

// static
std::string AntBlackboxLogger::RedactSensitive(std::string_view input) {
  std::string result(input);
  static constexpr std::array<std::string_view, 6> kKeys = {
      "token=", "password=", "passwd=", "secret=", "authorization:", "bearer "};
  for (std::string_view key : kKeys) {
    std::string lower = base::ToLowerASCII(result);
    size_t pos = 0;
    while ((pos = lower.find(key, pos)) != std::string::npos) {
      size_t val_start = pos + key.size();
      size_t val_end = result.find_first_of(" &\r\n\t\";", val_start);
      if (val_end == std::string::npos) {
        val_end = result.size();
      }
      result.replace(val_start, val_end - val_start, "[REDACTED]");
      lower = base::ToLowerASCII(result);
      pos = val_start + 10;
    }
  }
  return result;
}

void AntBlackboxLogger::EmitEvent(std::string_view event_name,
                                  std::string_view detail) const {
  if (!enabled_) {
    // Safe zero-overhead no-op when ANT_CHROMIUM_BLACKBOX_LOG is unset.
    return;
  }
#if BUILDFLAG(IS_WIN)
  const auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                          std::chrono::system_clock::now().time_since_epoch())
                          .count();
  const DWORD pid = ::GetCurrentProcessId();
  const std::string safe_detail = EscapeJsonString(RedactSensitive(detail));
  const std::string safe_event = EscapeJsonString(event_name);
  const std::string line =
      "{\"ts_ms\":" + std::to_string(now_ms) +
      ",\"pid\":" + std::to_string(pid) +
      ",\"event\":\"" + safe_event +
      "\",\"detail\":\"" + safe_detail + "\"}\n";
  std::wstring wide_path = base::UTF8ToWide(log_path_);
  HANDLE file = ::CreateFileW(wide_path.c_str(), FILE_APPEND_DATA,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                              OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (file != INVALID_HANDLE_VALUE) {
    DWORD written = 0;
    ::WriteFile(file, line.data(), static_cast<DWORD>(line.size()), &written,
                nullptr);
    ::CloseHandle(file);
  }
#endif
}

}  // namespace ant_chromium
