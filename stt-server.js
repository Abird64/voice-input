#!/usr/bin/env node
/**
 * 独立 SenseVoice STT 推理服务器
 * 复用 DSH 的 sherpa-onnx-node + SenseVoiceSmall 模型
 *
 * 启动: node stt-server.js
 * 接口: POST /transcribe?language=zh  (body: WAV bytes)
 * 返回: {"text": "识别文字", "audioSeconds": 3.5, "inferenceSeconds": 0.2}
 */

import { createServer } from 'node:http';
import { timingSafeEqual, randomBytes } from 'node:crypto';
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';

// ── 配置 ──────────────────────────────────────────────────
const PORT = parseInt(process.env.STT_PORT || '9876');
const TOKEN = process.env.STT_TOKEN || randomBytes(16).toString('hex');

// 模型路径 — 默认用 DSH 已下载的
const MODEL_ROOT = process.env.STT_MODEL_ROOT
  || `${process.env.HOME}/.dsh/speech-to-text/sensevoice/models`;

const MODEL_PATH = process.env.STT_MODEL_PATH
  || `${MODEL_ROOT}/sensevoice-onnx/model.int8.onnx`;

const TOKENS_PATH = process.env.STT_TOKENS_PATH
  || `${MODEL_ROOT}/sensevoice-onnx/tokens.txt`;

const VAD_PATH = process.env.STT_VAD_PATH
  || `${MODEL_ROOT}/silero/silero_vad.onnx`;

const THREADS = parseInt(process.env.STT_THREADS || '2');
const VAD_THRESHOLD = parseFloat(process.env.STT_VAD_THRESHOLD || '0.5');
const MIN_SPEECH_SEC = parseFloat(process.env.STT_MIN_SPEECH_SEC || '0.25');
const MIN_SILENCE_SEC = parseFloat(process.env.STT_MIN_SILENCE_SEC || '0.5');
const MAX_SEGMENT_SEC = parseInt(process.env.STT_MAX_SEGMENT_SEC || '30');
const MAX_AUDIO_BYTES = parseInt(process.env.STT_MAX_AUDIO_BYTES || `${4 * 1024 * 1024}`);
const VAD_ONSET_PADDING_SEC = parseFloat(process.env.STT_VAD_ONSET_PADDING || '0.3');

// 检查模型文件
for (const [name, path] of [['Model', MODEL_PATH], ['Tokens', TOKENS_PATH], ['VAD', VAD_PATH]]) {
  if (!existsSync(path)) {
    console.error(`❌ ${name} not found: ${path}`);
    console.error('   请先在 DSH 插件管理中启用并下载语音输入模型');
    process.exit(1);
  }
}

// ── 初始化 sherpa-onnx ────────────────────────────────────
const require_ = createRequire(import.meta.url);
const sherpaPath = process.env.SHERPA_ONNX_PATH
  || `${process.env.HOME}/.npm-global/lib/node_modules/@deepseek-ai/dsh/node_modules/sherpa-onnx-node`;
const sherpa = require_(sherpaPath);

const recognizer = new sherpa.OfflineRecognizer({
  featConfig: { sampleRate: 16000, featureDim: 80 },
  modelConfig: {
    senseVoice: {
      model: MODEL_PATH,
      language: 'auto',
      useInverseTextNormalization: 1,
    },
    tokens: TOKENS_PATH,
    numThreads: THREADS,
    provider: 'cpu',
    debug: 0,
  },
});

const detector = new sherpa.Vad(
  {
    sileroVad: {
      model: VAD_PATH,
      threshold: VAD_THRESHOLD,
      minSilenceDuration: MIN_SILENCE_SEC,
      minSpeechDuration: MIN_SPEECH_SEC,
      maxSpeechDuration: MAX_SEGMENT_SEC,
      windowSize: 512,
    },
    sampleRate: 16000,
    numThreads: THREADS,
    provider: 'cpu',
    debug: 0,
  },
  MAX_SEGMENT_SEC + MIN_SILENCE_SEC + 1,
);

// ── 推理函数 ──────────────────────────────────────────────
function transcribe(audioBuffer, language) {
  const audioSeconds = audioBuffer.byteLength / 32000; // 16kHz 16bit mono

  // WAV 校验
  if (audioBuffer.byteLength < 46 || audioBuffer.byteLength > MAX_AUDIO_BYTES) {
    throw Object.assign(new Error('Invalid audio size'), { code: 'invalid-input', status: 413 });
  }

  const pcm = new DataView(audioBuffer.buffer, audioBuffer.byteOffset + 44, audioBuffer.byteLength - 44);
  const samples = Float32Array.from(
    { length: pcm.byteLength / 2 },
    (_, i) => pcm.getInt16(i * 2, true) / 32768,
  );

  recognizer.setConfig({
    featConfig: { sampleRate: 16000, featureDim: 80 },
    modelConfig: {
      senseVoice: {
        model: MODEL_PATH,
        language: language || 'auto',
        useInverseTextNormalization: 1,
      },
      tokens: TOKENS_PATH,
      numThreads: THREADS,
      provider: 'cpu',
      debug: 0,
    },
  });

  detector.reset();

  const started = performance.now();
  const texts = [];
  const onsetPadding = Math.round(VAD_ONSET_PADDING_SEC * 16000);
  let isFirstSegment = true;

  const drain = () => {
    while (!detector.isEmpty()) {
      const segment = detector.front(false);
      let speech = segment.samples;

      if (isFirstSegment) {
        isFirstSegment = false;
        const from = Math.max(0, segment.start - onsetPadding);
        if (from < segment.start) {
          const restored = new Float32Array(segment.start - from + speech.length);
          restored.set(samples.subarray(from, segment.start));
          restored.set(speech, segment.start - from);
          speech = restored;
        }
      }

      const stream = recognizer.createStream();
      stream.acceptWaveform({ sampleRate: 16000, samples: speech });
      recognizer.decode(stream);
      const result = recognizer.getResult(stream);
      if (result.text) texts.push(result.text.trim());
      detector.pop();
    }
  };

  for (let offset = 0; offset < samples.length; offset += 512) {
    detector.acceptWaveform(samples.subarray(offset, offset + 512));
    drain();
  }
  detector.flush();
  drain();

  return {
    text: texts.filter(Boolean).join(' ').trim(),
    audioSeconds,
    inferenceSeconds: (performance.now() - started) / 1000,
  };
}

// ── HTTP 服务器 ───────────────────────────────────────────
const expected = Buffer.from(`Bearer ${TOKEN}`);

const server = createServer((req, res) => {
  const reply = (status, body) => {
    res.writeHead(status, { 'content-type': 'application/json' });
    res.end(JSON.stringify(body));
  };

  const auth = Buffer.from(req.headers.authorization || '');
  if (auth.length !== expected.length || !timingSafeEqual(auth, expected)) {
    req.resume();
    return reply(401, { error: 'Unauthorized' });
  }

  const url = new URL(req.url, 'http://localhost');
  if (req.method !== 'POST' || url.pathname !== '/transcribe') {
    req.resume();
    return reply(404, { error: 'Not found' });
  }

  const len = Number(req.headers['content-length']);
  if (!Number.isSafeInteger(len) || len < 46 || len > MAX_AUDIO_BYTES) {
    req.resume();
    return reply(413, { error: 'Invalid audio size', code: 'invalid-input' });
  }

  const chunks = [];
  req.on('data', (chunk) => chunks.push(chunk));
  req.on('end', () => {
    try {
      const audio = Buffer.concat(chunks);
      const lang = url.searchParams.get('language') || 'auto';
      const result = transcribe(audio, lang);
      reply(200, result);
    } catch (err) {
      reply(err.code === 'invalid-input' ? (err.status || 400) : 500, {
        error: err.message,
        ...(err.code === 'invalid-input' ? { code: 'invalid-input' } : {}),
      });
    }
  });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`🎤 STT Server ready — http://127.0.0.1:${PORT}`);
  console.log(`   Token: ${TOKEN}`);
  console.log(`   Model: ${MODEL_PATH}`);
});