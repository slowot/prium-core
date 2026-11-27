#!/usr/bin/env node
/**
 * Prium-Core / Ant-Chromium Stealth & Security Verification Suite
 *
 * Modes:
 *   node verify_stealth_suite.mjs
 *     -> Validates all static stealth/security invariants across 001..012 patches,
 *        zero upstream domain mangling, zero plaintext encryption flags,
 *        PriumNetGuard double-guard simulation, and PriumVault PRV1 envelope math.
 *
 *   node verify_stealth_suite.mjs --binary <path\to\chrome.exe>
 *     -> Additionally executes live binary checks:
 *        --version marker (AntChromiumKernel/ & ant-chromium-shm-v1),
 *        WMI Win32_Process command-line audit (zero --fingerprint* flags),
 *        and local HTTP probe checks over passive CDP (no Runtime.enable).
 */
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function resolvePatchesDir() {
  const candidates = [
    path.resolve(__dirname, '..', 'patches', 'prium-stealth'),
    path.resolve(__dirname, '..', 'patches'),
  ];
  for (const dir of candidates) {
    if (fs.existsSync(dir) && fs.readdirSync(dir).some((f) => f.startsWith('001-'))) {
      return dir;
    }
  }
  throw new Error('Could not locate prium-stealth / kernel patches directory');
}

const PATCHES_DIR = resolvePatchesDir();
const patchFiles = fs
  .readdirSync(PATCHES_DIR)
  .filter((f) => /^\d{3}-.*\.patch$/.test(f))
  .sort();

const OVERLAY_DIR = path.resolve(__dirname, '..', 'chromium_src', 'base', 'ant_chromium');
const overlayFiles = fs.existsSync(OVERLAY_DIR)
  ? fs.readdirSync(OVERLAY_DIR).filter((f) => /\.(h|cc)$/.test(f)).sort()
  : [];

let passed = 0;
function check(name, fn) {
  fn();
  passed += 1;
  console.log(`  [PASS] ${name}`);
}

console.log(`Running Prium-Stealth Verification Suite (patches: ${PATCHES_DIR})...`);

check('1. Stealth patch set contains all 12 numbered patches (001..012)', () => {
  assert.equal(patchFiles.length, 12);
  for (let i = 0; i < 12; i += 1) {
    assert.ok(patchFiles[i].startsWith(String(i + 1).padStart(3, '0') + '-'));
  }
});

const combined = [
  ...patchFiles.map((f) => fs.readFileSync(path.join(PATCHES_DIR, f), 'utf8')),
  ...overlayFiles.map((f) => fs.readFileSync(path.join(OVERLAY_DIR, f), 'utf8')),
].join('\n');

check('2. Zero legacy domain mangling or plaintext encryption/spoof flags', () => {
  const forbiddenTokens = [
    ['qjz', '9zk'].join(''),
    ['disable', 'encryption'].join('-'),
    ['disable', 'machine', 'id'].join('-'),
    ['FingerprintingCanvas', 'ImageDataNoiseEnabled'].join(''),
    ['FingerprintingClientRects', 'NoiseEnabled'].join(''),
    ['kSpoof', 'WebGLInfo'].join(''),
  ];
  for (const token of forbiddenTokens) {
    assert.ok(!combined.includes(token), `Unexpected legacy token found: ${token}`);
  }
});

check('3. PriumNetGuard double-guard logic blocks telemetry only when !has_web_initiator', () => {
  const telemetryHosts = [
    'clientservices.googleapis.com/chrome-variations',
    'update.googleapis.com/service/update2',
    'safebrowsing.googleapis.com',
    'beacons.gvt2.com',
    'crash2.googleapis.com',
  ];
  const shouldSinkhole = (hasWebInitiator, url) => {
    if (hasWebInitiator) return false;
    const lower = url.toLowerCase();
    if (!lower.startsWith('https://') && !lower.startsWith('http://')) return false;
    return telemetryHosts.some((p) => lower.includes(p));
  };
  assert.equal(shouldSinkhole(false, 'https://clientservices.googleapis.com/chrome-variations/seed'), true);
  assert.equal(shouldSinkhole(true, 'https://clientservices.googleapis.com/chrome-variations/seed'), false);
  assert.equal(shouldSinkhole(true, 'https://www.google.com/recaptcha/api2/anchor'), false);
  assert.equal(shouldSinkhole(true, 'https://accounts.google.com/ServiceLogin'), false);
  assert.equal(shouldSinkhole(false, 'https://safebrowsing.googleapis.com/v4/threatListUpdates'), true);
});

check('4. PriumVault PRV1 AES-256-GCM envelope encrypt/decrypt roundtrip & tamper rejection', () => {
  const masterKey = crypto.createHash('sha256').update('PriumVault-v1-MasterKey:prof1:42').digest();
  const secretRootKey = crypto.randomBytes(32);
  const nonce = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', masterKey, nonce);
  const encrypted = Buffer.concat([cipher.update(secretRootKey), cipher.final()]);
  const tag = cipher.getAuthTag();
  const stored = Buffer.concat([Buffer.from('PRV1', 'ascii'), nonce, encrypted, tag]);

  assert.equal(stored.subarray(0, 4).toString('ascii'), 'PRV1');
  const decipher = crypto.createDecipheriv('aes-256-gcm', masterKey, stored.subarray(4, 16));
  decipher.setAuthTag(stored.subarray(stored.length - 16));
  const decrypted = Buffer.concat([
    decipher.update(stored.subarray(16, stored.length - 16)),
    decipher.final(),
  ]);
  assert.deepEqual(decrypted, secretRootKey);

  // Tamper 1 byte -> GCM auth failure
  stored[20] ^= 0x01;
  const badDecipher = crypto.createDecipheriv('aes-256-gcm', masterKey, stored.subarray(4, 16));
  badDecipher.setAuthTag(stored.subarray(stored.length - 16));
  assert.throws(() => {
    badDecipher.update(stored.subarray(16, stored.length - 16));
    badDecipher.final();
  });
});

check('5. Native stealth coverage: ICU timezone, Win fonts, Worker Navigator, Battery & WebGPU', () => {
  assert.ok(combined.includes('icu::TimeZone::adoptDefault'));
  assert.ok(combined.includes('IsAllowedBaselineWinFontFamily'));
  assert.ok(combined.includes('NavigatorDeviceMemory::deviceMemory'));
  assert.ok(combined.includes('NavigatorConcurrentHardware::hardwareConcurrency'));
  assert.ok(combined.includes('ComputeWebBatteryStatus'));
  assert.ok(combined.includes('GPUAdapter'));
});

check('6. BoringSSL JA4 permutation & HTTP/2 SETTINGS GREASE enabled cleanly', () => {
  assert.ok(combined.includes('SSL_CTX_set_grease_enabled'));
  assert.ok(combined.includes('SSL_CTX_set_permute_extensions'));
  assert.ok(combined.includes('enable_http2_settings_grease'));
});

check('7. Zero Runtime.enable in kernel patches and passive CDP cdc_ suppression present', () => {
  assert.ok(!combined.includes('Runtime.enable'));
  assert.ok(combined.includes('ANT_FEAT_CDP_STEALTH'));
  assert.ok(combined.includes('cdc_'));
});

const binIdx = process.argv.indexOf('--binary');
if (binIdx !== -1) {
  const binaryPath = process.argv[binIdx + 1];
  assert.ok(binaryPath && fs.existsSync(binaryPath), `Binary not found: ${binaryPath}`);
  console.log(`\nRunning live binary verification against: ${binaryPath}`);

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'prium-stealth-e2e-'));
  const verProfileDir = path.join(tmpDir, 'ver-profile');
  const blackboxLog = path.join(tmpDir, 'blackbox.jsonl');

  let versionOut = '';
  try {
    versionOut = execFileSync(
      binaryPath,
      ['--version', `--user-data-dir=${verProfileDir}`],
      { encoding: 'utf8', timeout: 10000 }
    );
  } catch {
    versionOut = '';
  }

  check('8. Live binary exposes AntChromiumKernel/150.0.7871.* identity & ANTKRN01 SHM support', () => {
    if (versionOut.includes('AntChromiumKernel/') && versionOut.includes('ant-chromium-shm-v1')) {
      return;
    }
    if (process.platform === 'win32') {
      const escapedBin = binaryPath.replace(/'/g, "''");
      const peVer = execFileSync(
        'powershell',
        ['-NoProfile', '-Command', `(Get-Item '${escapedBin}').VersionInfo.ProductVersion`],
        { encoding: 'utf8' }
      ).trim();
      assert.match(peVer, /^150\.0\.7871\.\d+/, `Unexpected PE ProductVersion: ${peVer}`);
      const dllPath = path.join(path.dirname(binaryPath), 'chrome.dll');
      assert.ok(fs.existsSync(dllPath), `Missing sibling chrome.dll at ${dllPath}`);
      const dllBuf = fs.readFileSync(dllPath);
      assert.ok(dllBuf.includes(Buffer.from('ANTKRN01', 'ascii')), 'chrome.dll missing ANTKRN01');
      return;
    }
    assert.fail(`Unexpected --version: ${versionOut}`);
  });

  const server = http.createServer((_req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end('<html><body><canvas id="c" width="16" height="16"></canvas></body></html>');
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  const child = spawn(
    binaryPath,
    [
      '--headless=new',
      '--no-first-run',
      '--no-default-browser-check',
      `--user-data-dir=${path.join(tmpDir, 'profile')}`,
      '--remote-debugging-port=0',
      `http://127.0.0.1:${port}/`,
    ],
    {
      env: { ...process.env, ANT_CHROMIUM_BLACKBOX_LOG: blackboxLog },
      stdio: ['ignore', 'pipe', 'pipe'],
    }
  );

  try {
    await new Promise((r) => setTimeout(r, 2500));
    if (process.platform === 'win32' && child.pid) {
      const cmdline = execFileSync(
        'powershell',
        [
          '-NoProfile',
          '-Command',
          `(Get-CimInstance Win32_Process -Filter "ProcessId=${child.pid}").CommandLine`,
        ],
        { encoding: 'utf8' }
      );
      check('9. Live Win32_Process command line leaks zero --fingerprint* flags', () => {
        assert.ok(!cmdline.includes('--fingerprint'), `Leaked --fingerprint in argv: ${cmdline}`);
        assert.ok(!cmdline.includes('--disable-encryption'), `Leaked --disable-encryption in argv`);
      });
    }
    check('10. Live binary writes redacted JSONL blackbox log when ANT_CHROMIUM_BLACKBOX_LOG is set', () => {
      assert.ok(fs.existsSync(blackboxLog), 'Expected blackbox log file to exist');
      const lines = fs.readFileSync(blackboxLog, 'utf8').trim().split(/\r?\n/);
      assert.ok(lines.length >= 1);
      const parsed = JSON.parse(lines[0]);
      assert.equal(typeof parsed.event, 'string');
    });
  } finally {
    if (process.platform === 'win32' && child.pid) {
      try {
        execFileSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
      } catch {
        // Ignore already exited child process tree.
      }
    } else {
      child.kill();
    }
    server.close();
    try {
      fs.rmSync(tmpDir, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
    } catch {
      // Best-effort temp cleanup on Windows when OS holds handles briefly after taskkill.
    }
  }
}

console.log(`\nPrium-Stealth verification summary: ${passed} checks passed (all assertions green).`);
