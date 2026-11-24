# Local voice detection assets

- `ort.wasm.min.mjs` and `ort-wasm-simd-threaded.{mjs,wasm}` are unmodified from `onnxruntime-web@1.22.0` (MIT).
- `vad.worklet.bundle.min.js` (ISC) and `silero_vad_v5.onnx` (MIT) are unmodified from `@ricky0123/vad-web@0.0.29`.

The browser loads these assets from the same origin as the voice console, either directly on loopback or through the LAN HTTPS gateway. No CDN is used at runtime. The corresponding licenses and third-party notices are in this directory.
