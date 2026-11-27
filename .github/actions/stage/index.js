import * as core from '@actions/core';
import * as io from '@actions/io';
import * as exec from '@actions/exec';
import { DefaultArtifactClient } from '@actions/artifact';
import * as glob from '@actions/glob';
import fs from 'node:fs';
import path from 'node:path';

const BUILD_TIMEOUT_EXIT_CODE = 124;
const WORKDIR = 'C:\\prium-core';
const BUILD_DIR = path.join(WORKDIR, 'build');
const SRC_DIR = path.join(BUILD_DIR, 'src');

function normalizeR2Endpoint(raw) {
    const trimmed = (raw || '').trim().replace(/\/+$/, '');
    if (!trimmed) {
        return '';
    }
    const withScheme = /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
    try {
        const u = new URL(withScheme);
        return `${u.protocol}//${u.host}`;
    } catch {
        return withScheme;
    }
}

async function setupSccacheEnv() {
    const endpoint = normalizeR2Endpoint(process.env.R2_ENDPOINT);
    const accessKey = (process.env.R2_ACCESS_KEY_ID || process.env.AWS_ACCESS_KEY_ID || '').trim();
    const secretKey = (process.env.R2_SECRET_ACCESS_KEY || process.env.AWS_SECRET_ACCESS_KEY || '').trim();
    if (!endpoint || !accessKey || !secretKey) {
        console.log('R2 credentials/endpoint not configured; skipping sccache R2 server startup.');
        return false;
    }

    process.env.AWS_ACCESS_KEY_ID = accessKey;
    process.env.AWS_SECRET_ACCESS_KEY = secretKey;
    process.env.SCCACHE_BUCKET = 'prium-sccache';
    process.env.SCCACHE_REGION = 'auto';
    process.env.SCCACHE_S3_KEY_PREFIX = 'prium/win64/';
    process.env.SCCACHE_ENDPOINT = endpoint;
    process.env.SCCACHE_S3_USE_SSL = endpoint.startsWith('https://') ? 'true' : 'false';
    process.env.SCCACHE_CACHE_COMPRESSION = 'zstd';

    const startCode = await exec.exec('sccache', ['--start-server'], { ignoreReturnCode: true });
    console.log('--- sccache stats (before build) ---');
    const statsCode = await exec.exec('sccache', ['--show-stats'], { ignoreReturnCode: true });
    if (startCode !== 0 || statsCode !== 0) {
        console.warn(`sccache R2 health check failed (start=${startCode}, stats=${statsCode}); falling back to local compilation.`);
        delete process.env.SCCACHE_BUCKET;
        return false;
    }
    return true;
}

async function uploadNamedArtifact(artifact, name, files, rootDir, retentionDays) {
    for (let i = 0; i < 5; ++i) {
        try {
            await artifact.deleteArtifact(name);
        } catch {
            // ignore missing artifact
        }
        try {
            await artifact.uploadArtifact(name, files, rootDir, {
                retentionDays,
                compressionLevel: 0,
            });
            return;
        } catch (err) {
            console.error(`Upload artifact ${name} failed (attempt ${i + 1}/5): ${err}`);
            await new Promise((r) => setTimeout(r, 10000));
        }
    }
    throw new Error(`Failed to upload artifact ${name} after 5 attempts.`);
}

async function run() {
    process.on('SIGINT', () => {});

    const finished = core.getBooleanInput('finished', { required: true });
    const fromArtifact = core.getBooleanInput('from_artifact', { required: true });
    const trackRaw = (core.getInput('track', { required: false }) || 'release').trim().toLowerCase();
    const track = trackRaw === 'dev' ? 'dev' : 'release';
    const resumeRunId = (core.getInput('resume_run_id', { required: false }) || '').trim();

    console.log(`finished=${finished}, from_artifact=${fromArtifact}, track=${track}, resume_run_id=${resumeRunId}`);
    if (finished) {
        core.setOutput('finished', true);
        return;
    }

    const artifact = new DefaultArtifactClient();
    const stageArtifactName = `build-artifact-${track}`;

    if (fromArtifact) {
        const artifactInfo = await artifact.getArtifact(stageArtifactName);
        await io.mkdirP(BUILD_DIR);
        await artifact.downloadArtifact(artifactInfo.artifact.id, { path: BUILD_DIR });
        await exec.exec('7z', ['x', path.join(BUILD_DIR, 'artifacts.zip'), `-o${BUILD_DIR}`, '-y']);
        await io.rmRF(path.join(BUILD_DIR, 'artifacts.zip'));
    } else if (resumeRunId.length > 0) {
        const repo = process.env.GITHUB_REPOSITORY || 'slowot/prium-core';
        console.log(`Seeding build from prior run ${resumeRunId} (${repo}) artifact ${stageArtifactName}...`);
        await io.mkdirP(BUILD_DIR);
        const dlCode = await exec.exec('gh', [
            'run', 'download', resumeRunId, '-R', repo, '-n', stageArtifactName, '-D', BUILD_DIR,
        ]);
        if (dlCode !== 0) {
            throw new Error(`Failed to download ${stageArtifactName} from run ${resumeRunId}`);
        }
        await exec.exec('7z', ['x', path.join(BUILD_DIR, 'artifacts.zip'), `-o${BUILD_DIR}`, '-y']);
        await io.rmRF(path.join(BUILD_DIR, 'artifacts.zip'));
    }

    const sccacheEnabled = await setupSccacheEnv();

    if (!fs.existsSync(path.join(SRC_DIR, 'BUILD.gn'))) {
        const syncCode = await exec.exec('python', ['scripts/sync_chromium.py', '--track', track, '--cleanup-cache'], {
            cwd: WORKDIR,
            ignoreReturnCode: true,
        });
        if (syncCode !== 0) {
            core.setFailed(`sync_chromium.py failed with exit code ${syncCode}`);
            return;
        }
    }

    const buildArgs = ['scripts/build_prium.py', '--ci', '--track', track, '-j', '4'];
    const retCode = await exec.exec('python', buildArgs, {
        cwd: WORKDIR,
        ignoreReturnCode: true,
    });

    if (sccacheEnabled) {
        console.log('--- sccache stats (after build) ---');
        await exec.exec('sccache', ['--show-stats'], { ignoreReturnCode: true });
    }

    if (retCode === 0) {
        core.setOutput('finished', true);

        const pkgGlob = await glob.create(path.join(BUILD_DIR, 'ant-chromium_*'), { matchDirectories: false });
        const packageList = await pkgGlob.glob();
        if (packageList.length === 0) {
            core.setFailed('build_prium.py exited 0 but no ant-chromium_* package artifact was produced.');
            return;
        }
        await uploadNamedArtifact(artifact, 'ant-chromium-win64', packageList, BUILD_DIR, 30);

        const symGlob = await glob.create(path.join(BUILD_DIR, 'prium-core-symbols_*'), { matchDirectories: false });
        const symbolList = await symGlob.glob();
        if (symbolList.length > 0) {
            await uploadNamedArtifact(artifact, 'prium-core-symbols-win64', symbolList, BUILD_DIR, 30);
        }
    } else if (retCode === BUILD_TIMEOUT_EXIT_CODE) {
        await new Promise((r) => setTimeout(r, 5000));
        const archiveZip = path.join(WORKDIR, 'artifacts.zip');
        await exec.exec('7z', ['a', '-tzip', archiveZip, SRC_DIR, '-mx=3', '-mtc=on'], {
            ignoreReturnCode: true,
        });
        await uploadNamedArtifact(artifact, stageArtifactName, [archiveZip], WORKDIR, 4);
        core.setOutput('finished', false);
    } else {
        core.setFailed(`Prium-Core build failed with non-timeout exit code ${retCode}`);
    }
}

run().catch((err) => core.setFailed(err.message));
